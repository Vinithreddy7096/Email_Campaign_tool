#!/usr/bin/env python3
"""
Nightly Postgres backups and an automated restore test.

    pgbackup.py backup         dump, checksum, upload, prune
    pgbackup.py restore-check  fetch the newest dump, restore it into a scratch
                               database, prove the data is there, drop it

Runs in the backup image (deploy/backup/Dockerfile: postgres 16 client tools
+ Python). Configuration from the environment:

  DATABASE_URL            the database to back up (backup) or a server where a
                          scratch database may be created (restore-check;
                          use a separate server in production, never the primary)
  BACKUP_S3_BUCKET        S3 / MinIO bucket   ┐ one of these two
  BACKUP_DIR              a local directory   ┘
  BACKUP_S3_PREFIX        default "postgres/<BACKUP_ENV>"
  BACKUP_ENV              default "production"
  S3_ENDPOINT_URL         for MinIO; unset for AWS
  BACKUP_SSE              "AES256" (default) or "aws:kms" (+ BACKUP_KMS_KEY_ID)
  BACKUP_RETENTION_DAYS   keep every dump this long (default 14)
  BACKUP_KEEP_MONTHLY     also keep the first dump of each of the last N months (default 12)
  BACKUP_METRICS_FILE     node_exporter textfile, e.g. /var/lib/node_exporter/textfile/pgbackup.prom

A restore test passes only if the append-only tables (audit, consent,
suppression, events, policy decisions, job history) have at least as many rows
as the manifest recorded just before the dump: those tables never lose rows,
so fewer means the backup is incomplete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Rows in these are never deleted (DB triggers enforce it), so a restore must
# have at least the counts taken just before the dump.
APPEND_ONLY_TABLES = (
    "audit_event",
    "consent_record",
    "suppression",
    "events_delivery_event",
    "delivery_policy_decision",
    "delivery_job_transition",
    "delivery_send_attempt",
    "delivery_message",
)
KEY_RE = re.compile(r"mailer-(\d{8}T\d{6}Z)\.dump$")
METRIC_HELP = {
    "pgbackup_last_success_timestamp_seconds": "Last successful backup (unix time).",
    "pgbackup_last_size_bytes": "Size of the last backup in bytes.",
    "pgbackup_last_duration_seconds": "Duration of the last backup.",
    "pgbackup_restore_check_last_success_timestamp_seconds": "Last successful restore test (unix time).",
}


# ─── Connection ──────────────────────────────────────────────────────────


def pg_env(url: str, dbname: str | None = None) -> dict[str, str]:
    """libpq environment for ``url`` — keeps the password out of the process list."""
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise SystemExit(f"DATABASE_URL must be postgres://…, got {u.scheme}://")
    env = dict(os.environ)
    env.update({
        "PGHOST": u.hostname or "localhost",
        "PGPORT": str(u.port or 5432),
        "PGUSER": unquote(u.username or ""),
        "PGPASSWORD": unquote(u.password or ""),
        "PGDATABASE": dbname or u.path.lstrip("/") or "postgres",
    })  # fmt: skip
    return env


def psql(env: dict[str, str], sql: str) -> str:
    out = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],  # noqa: S603, S607 — fixed argv, our SQL
                         env=env, check=True, capture_output=True, text=True)  # fmt: skip
    return out.stdout.strip()


def row_counts(env: dict[str, str]) -> dict[str, int]:
    present = set(
        psql(env, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").splitlines()
    )
    counts = {}
    for table in APPEND_ONLY_TABLES:
        if table in present:
            # Table names come from APPEND_ONLY_TABLES, never from input.
            sql = f'SELECT count(*) FROM "{table}"'  # noqa: S608
            counts[table] = int(psql(env, sql))
    return counts


# ─── Storage ─────────────────────────────────────────────────────────────


class Store:
    def put(self, local: Path, key: str, metadata: dict[str, str]) -> None: ...
    def get(self, key: str, local: Path) -> None: ...
    def names(self) -> list[str]: ...
    def delete(self, key: str) -> None: ...


@dataclass
class DirStore(Store):
    root: Path

    def put(self, local: Path, key: str, metadata: dict[str, str]) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)

    def get(self, key: str, local: Path) -> None:
        shutil.copyfile(self.root / key, local)

    def names(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(str(p.relative_to(self.root)).replace(os.sep, "/")
                      for p in self.root.rglob("*") if p.is_file())  # fmt: skip

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3Store(Store):
    def __init__(self, bucket: str, prefix: str) -> None:
        import boto3

        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def put(self, local: Path, key: str, metadata: dict[str, str]) -> None:
        extra: dict[str, Any] = {"Metadata": metadata}
        sse = os.environ.get("BACKUP_SSE", "AES256")
        if sse and not (os.environ.get("S3_ENDPOINT_URL") and sse == "AES256"):
            # MinIO without a KMS rejects SSE-S3 headers; AWS gets them always.
            extra["ServerSideEncryption"] = sse
            if sse == "aws:kms" and os.environ.get("BACKUP_KMS_KEY_ID"):
                extra["SSEKMSKeyId"] = os.environ["BACKUP_KMS_KEY_ID"]
        self.s3.upload_file(str(local), self.bucket, self._key(key), ExtraArgs=extra)

    def get(self, key: str, local: Path) -> None:
        self.s3.download_file(self.bucket, self._key(key), str(local))

    def names(self) -> list[str]:
        out: list[str] = []
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix + "/"
        )
        for page in pages:
            out += [o["Key"][len(self.prefix) + 1 :] for o in page.get("Contents", [])]
        return sorted(out)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(key))


def store_from_env() -> Store:
    bucket, directory = os.environ.get("BACKUP_S3_BUCKET"), os.environ.get("BACKUP_DIR")
    if bool(bucket) == bool(directory):
        raise SystemExit("set exactly one of BACKUP_S3_BUCKET or BACKUP_DIR")
    prefix = (
        os.environ.get("BACKUP_S3_PREFIX")
        or f"postgres/{os.environ.get('BACKUP_ENV', 'production')}"
    )
    return S3Store(bucket, prefix) if bucket else DirStore(Path(directory or "") / prefix)


# ─── Helpers ─────────────────────────────────────────────────────────────


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp_of(key: str) -> datetime | None:
    m = KEY_RE.search(key)
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if m else None


def write_metric(name: str, value: float) -> None:
    """Prometheus textfile collector (alerts: monitoring/prometheus/rules)."""
    path = os.environ.get("BACKUP_METRICS_FILE")
    if not path:
        return
    target = Path(path)
    values: dict[str, str] = {}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                metric, _, value_text = line.partition(" ")
                values[metric] = value_text
    values[name] = repr(float(value))
    body = "".join(f"# HELP {k} {METRIC_HELP.get(k, k)}\n# TYPE {k} gauge\n{k} {v}\n"
                   for k, v in sorted(values.items()))  # fmt: skip
    tmp = target.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)


# ─── backup ──────────────────────────────────────────────────────────────


def to_prune(keys: list[str], now: datetime, retention_days: int, keep_monthly: int) -> list[str]:
    """Dumps (and their manifests) older than the retention window, except the
    first dump of each of the last ``keep_monthly`` months. The newest dump is
    never pruned."""
    dumps = sorted((when, k) for k in keys if (when := stamp_of(k)) is not None)
    if not dumps:
        return []
    keep: set[str] = {dumps[-1][1]}
    cutoff = now - timedelta(days=retention_days)
    first_of_month: dict[tuple[int, int], str] = {}
    for when, key in dumps:
        first_of_month.setdefault((when.year, when.month), key)
        if when >= cutoff:
            keep.add(key)
    months = sorted(first_of_month)[-keep_monthly:] if keep_monthly > 0 else []
    keep.update(first_of_month[m] for m in months)
    out = []
    for _, key in dumps:
        if key not in keep:
            out += [key, key.removesuffix(".dump") + ".json"]
    return out


def backup(args: argparse.Namespace) -> int:
    started = time.time()
    env = pg_env(os.environ["DATABASE_URL"])
    store = store_from_env()
    now = datetime.now(UTC)
    key = now.strftime("%Y/%m/%d/mailer-%Y%m%dT%H%M%SZ.dump")
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "mailer.dump"
        counts = row_counts(env)  # before the dump: the dump can only have more
        subprocess.run(["pg_dump", "--format=custom", "--compress=6", "--no-password",  # noqa: S603, S607
                        f"--file={dump}"], env=env, check=True, capture_output=True, text=True)  # fmt: skip
        digest = sha256(dump)
        version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True,  # noqa: S607
                                 check=True).stdout.strip()  # fmt: skip
        manifest = {"created_at": now.isoformat(), "key": key, "sha256": digest,
                    "size_bytes": dump.stat().st_size, "row_counts": counts, "pg_dump_version": version}  # fmt: skip
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        store.put(dump, key, {"sha256": digest})
        store.put(manifest_path, key.removesuffix(".dump") + ".json", {})
    pruned = to_prune(store.names(), now, args.retention_days, args.keep_monthly)
    for old in pruned:
        store.delete(old)
    write_metric("pgbackup_last_success_timestamp_seconds", time.time())
    write_metric("pgbackup_last_size_bytes", manifest["size_bytes"])
    write_metric("pgbackup_last_duration_seconds", time.time() - started)
    print(json.dumps({"backup": key, "sha256": digest, "size_bytes": manifest["size_bytes"],
                      "row_counts": counts, "pruned": len(pruned) // 2}))  # fmt: skip
    return 0


# ─── restore-check ───────────────────────────────────────────────────────


def restore_check(args: argparse.Namespace) -> int:
    store = store_from_env()
    dumps = [k for k in store.names() if KEY_RE.search(k)]
    if not dumps:
        print("restore-check: no backups found", file=sys.stderr)
        return 1
    key = args.key or dumps[-1]
    scratch = f"restore_check_{int(time.time())}"
    admin = pg_env(os.environ["DATABASE_URL"], dbname="postgres")
    target = pg_env(os.environ["DATABASE_URL"], dbname=scratch)
    problems: list[str] = []
    restored: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        dump, manifest_path = Path(tmp) / "mailer.dump", Path(tmp) / "manifest.json"
        store.get(key, dump)
        store.get(key.removesuffix(".dump") + ".json", manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sha256(dump) != manifest["sha256"]:
            print(f"restore-check: {key} checksum does not match its manifest", file=sys.stderr)
            return 1
        psql(admin, f'CREATE DATABASE "{scratch}"')
        try:
            subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",  # noqa: S603, S607
                            f"--dbname={scratch}", str(dump)], env=target, check=True,
                           capture_output=True, text=True)  # fmt: skip
            restored = row_counts(target)
            for table, expected in manifest["row_counts"].items():
                if restored.get(table, -1) < expected:
                    problems.append(
                        f"{table}: {restored.get(table)} rows, manifest says ≥ {expected}"
                    )
            if int(psql(target, "SELECT count(*) FROM django_migrations")) == 0:
                problems.append("django_migrations is empty")
        finally:
            psql(admin, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
    if problems:
        print("restore-check FAILED for " + key + ":\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    write_metric("pgbackup_restore_check_last_success_timestamp_seconds", time.time())
    print(json.dumps({"restore_check": key, "status": "ok", "row_counts": restored}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Postgres backups and restore tests.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument(
        "--retention-days", type=int, default=int(os.environ.get("BACKUP_RETENTION_DAYS", "14"))
    )
    b.add_argument(
        "--keep-monthly", type=int, default=int(os.environ.get("BACKUP_KEEP_MONTHLY", "12"))
    )
    r = sub.add_parser("restore-check")
    r.add_argument("--key", help="a specific dump (default: the newest)")
    args = parser.parse_args(argv)
    try:
        return backup(args) if args.command == "backup" else restore_check(args)
    except subprocess.CalledProcessError as exc:
        print(f"pgbackup: {' '.join(map(str, exc.cmd[:2]))} failed with {exc.returncode}: "
              f"{(exc.stderr or '').strip()}", file=sys.stderr)  # fmt: skip
        return 1


if __name__ == "__main__":
    sys.exit(main())
