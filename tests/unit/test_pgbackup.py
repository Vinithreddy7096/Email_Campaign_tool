"""scripts/backup/pgbackup.py retention: prune old dumps, never the newest,
keep the first dump of each recent month. (Backup and restore themselves are
exercised against a real Postgres in CI job "backup".)"""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pgbackup", Path(__file__).resolve().parents[2] / "scripts" / "backup" / "pgbackup.py"
)
assert spec and spec.loader
pgbackup = importlib.util.module_from_spec(spec)
sys.modules["pgbackup"] = pgbackup
spec.loader.exec_module(pgbackup)

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


def key(day):
    return day.strftime("%Y/%m/%d/mailer-%Y%m%dT%H%M%SZ.dump")


def nightly(days):
    return [key(NOW - timedelta(days=d)) for d in range(days)]


def test_recent_dumps_are_kept():
    assert pgbackup.to_prune(nightly(10), NOW, retention_days=14, keep_monthly=12) == []


def test_old_dumps_and_their_manifests_are_pruned_but_month_starts_kept():
    keys = nightly(120)
    pruned = pgbackup.to_prune(keys, NOW, retention_days=14, keep_monthly=12)
    kept = {k for k in keys if k not in pruned}
    assert key(NOW) in kept and key(NOW - timedelta(days=13)) in kept
    assert key(datetime(2026, 7, 1, 3, 0, tzinfo=UTC)) in kept  # first of July
    assert key(datetime(2026, 7, 2, 3, 0, tzinfo=UTC)) in pruned
    assert key(datetime(2026, 7, 2, 3, 0, tzinfo=UTC)).removesuffix(".dump") + ".json" in pruned


def test_the_newest_dump_is_never_pruned():
    old = [key(NOW - timedelta(days=400))]
    assert pgbackup.to_prune(old, NOW, retention_days=1, keep_monthly=0) == []


def test_monthly_keep_is_limited():
    keys = nightly(400)
    pruned = set(pgbackup.to_prune(keys, NOW, retention_days=14, keep_monthly=3))
    month_starts = [k for k in keys if k.split("/")[2] == "01" and k not in pruned]
    assert len(month_starts) == 3


def test_unrelated_files_are_left_alone():
    assert pgbackup.to_prune(["README.txt", "2026/01/01/other.bin"], NOW, 1, 0) == []


def test_password_stays_out_of_the_command_line():
    env = pgbackup.pg_env("postgres://u:p%40ss@db.internal:5433/mailer")
    assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGPASSWORD"], env["PGDATABASE"]) == (
        "db.internal", "5433", "u", "p@ss", "mailer")  # fmt: skip
