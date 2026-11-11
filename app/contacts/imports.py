"""Contact import from a CSV file (§6.3).

Two steps, so the web request stays fast and a slow file cannot time it out:

1. ``start(actor, ...)`` — in the request. Checks permission, size and the
   attestation, stores the bytes (``ContactImportFile``) and a PENDING
   ``ContactImport``, and queues ``contacts.process_import``.
2. ``process(import_id)`` — in a worker. Parses and validates every row first,
   then writes everything in **one transaction**: an import is applied whole or
   not at all, so a crash or a rerun can never leave half a file imported.

What a row does (fail closed at every step):

- invalid address, bad ``consent_date``, over-long name → skipped, counted
- same address twice in the file, or already on the list → skipped (duplicate)
- suppressed for this list's stream (any scope that blocks the send) → skipped
- previously withdrew marketing consent, or was erased → skipped (opted out);
  a file is never allowed to re-subscribe someone who left
- otherwise: the contact is created if new (an existing contact's details are
  **not** overwritten by a file), added to the list, and — for marketing lists
  — given a list-scoped GRANTED consent record (method IMPORT) carrying the
  attestation, the file's SHA-256 and the row number.

Purchased, scraped, harvested or rented lists are prohibited (§0.2). The
attestation is the record that a named person declared this file is not one.

CSV format: a header row; an ``email`` column is required. Recognised optional
columns: ``first_name``, ``last_name``, ``consent_date`` (ISO date or datetime
the person opted in; defaults to the time of attestation). Every other column
becomes a contact attribute (names are lowercased, spaces become ``_``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from audit.models import AuditEvent
from common.email import InvalidEmailError, email_hash, normalize_email
from common.models import Stream
from consent.evaluation import CONSENT_WITHDRAWN, marketing_consent_bulk
from consent.models import ConsentRecord
from contacts.models import (
    Contact,
    ContactImport,
    ContactImportFile,
    ContactList,
    ListMembership,
)
from suppression.models import Suppression

log = logging.getLogger(__name__)
Status = ContactImport.Status

_ALIASES = {
    "email": "email", "email_address": "email", "e_mail": "email", "mail": "email",
    "first_name": "first_name", "firstname": "first_name", "given_name": "first_name",
    "last_name": "last_name", "lastname": "last_name", "surname": "last_name",
    "family_name": "last_name",
    "consent_date": "consent_date", "consented_at": "consent_date", "opt_in_date": "consent_date",
    "opted_in_at": "consent_date",
}  # fmt: skip
_HEADER = re.compile(r"^[a-z0-9_]{1,64}$")
_NAME_MAX = 150
_BATCH = 2000


class ImportRejected(Exception):
    """The file as a whole cannot be imported (format, size, header)."""


@dataclass
class _Row:
    line: int
    email: str
    hashed: str
    first_name: str
    last_name: str
    consent_at: datetime | None
    attributes: dict[str, str]


@dataclass
class _Tally:
    total: int = 0
    imported: int = 0
    invalid: int = 0
    duplicate: int = 0
    suppressed: int = 0
    opted_out: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def issue(self, line: int, code: str, detail: str) -> None:
        if len(self.issues) < ContactImport.ISSUE_LIMIT:
            self.issues.append({"row": line, "code": code, "detail": detail})


# ─── Step 1: accept the upload ───────────────────────────────────────────


@transaction.atomic
def start(
    actor: Any,
    *,
    contact_list: ContactList,
    filename: str,
    content: bytes,
    consent_source: str,
    attestation_text: str,
) -> ContactImport:
    if not actor.has_perm("contacts.add_contactimport"):
        raise PermissionDenied("You may not import contacts.")
    if contact_list.archived_at is not None:
        raise ValidationError(f"List {contact_list} is archived.")
    consent_source, attestation_text = consent_source.strip(), attestation_text.strip()
    if not consent_source or not attestation_text:
        raise ValidationError("Say where consent came from and attest to it; both are required.")
    if not content:
        raise ValidationError("The file is empty.")
    if len(content) > settings.CONTACT_IMPORT_MAX_BYTES:
        raise ValidationError(
            f"The file is {len(content):,} bytes; the limit is "
            f"{settings.CONTACT_IMPORT_MAX_BYTES:,} (CONTACT_IMPORT_MAX_BYTES)."
        )
    digest = hashlib.sha256(content).hexdigest()
    imp = ContactImport.objects.create(
        contact_list=contact_list,
        file_object_key=f"db:contacts_import_file:sha256:{digest}",
        original_filename=(filename or "upload.csv")[:255],
        consent_source=consent_source,
        attestation_text=attestation_text,
        attested_by=actor,
        attested_at=timezone.now(),
    )
    ContactImportFile.objects.create(
        contact_import=imp, content=content, size=len(content), sha256=digest
    )
    AuditEvent.record(
        "contact_import.create", actor=actor, target=imp,
        after={"list": str(contact_list.pk), "file": imp.original_filename, "sha256": digest,
               "bytes": len(content), "consent_source": consent_source},
    )  # fmt: skip

    from workers.tasks.contacts import process_import

    transaction.on_commit(lambda: process_import.delay(str(imp.pk)))
    return imp


# ─── Step 2: process it ──────────────────────────────────────────────────


def process(import_id: Any) -> ContactImport:
    """Idempotent: a finished import is returned untouched; an interrupted
    one (VALIDATING/IMPORTING with nothing committed) is simply run again."""
    with transaction.atomic():
        imp = ContactImport.objects.select_for_update().get(pk=import_id)
        if imp.is_finished:
            return imp
        imp.status = Status.VALIDATING
        imp.save(update_fields=["status", "updated_at"])

    try:
        content = bytes(ContactImportFile.objects.get(contact_import=imp).content)
        tally = _Tally()
        rows = _parse(content, tally)
        _set_status(imp, Status.IMPORTING, total_rows=tally.total)
        with transaction.atomic():
            imp = ContactImport.objects.select_for_update().get(pk=import_id)
            if imp.is_finished:  # another worker finished it meanwhile
                return imp
            _apply(imp, rows, tally)
            imp.status = Status.COMPLETED
            imp.total_rows = tally.total
            imp.imported_count = tally.imported
            imp.skipped_invalid = tally.invalid
            imp.skipped_duplicate = tally.duplicate
            imp.skipped_suppressed = tally.suppressed
            imp.skipped_opted_out = tally.opted_out
            imp.issues = tally.issues
            imp.error = ""
            imp.completed_at = timezone.now()
            imp.save()
            AuditEvent.record("contact_import.complete", target=imp, actor_label="system",
                              after=_counts(imp))  # fmt: skip
        return imp
    except ImportRejected as exc:
        return _finish_unapplied(import_id, Status.REJECTED, str(exc))
    except Exception as exc:
        log.exception("contact import %s failed", import_id)
        return _finish_unapplied(import_id, Status.FAILED, f"{exc.__class__.__name__}: {exc}")


def _counts(imp: ContactImport) -> dict[str, int]:
    return {
        "total_rows": imp.total_rows,
        "imported": imp.imported_count,
        "skipped_invalid": imp.skipped_invalid,
        "skipped_duplicate": imp.skipped_duplicate,
        "skipped_suppressed": imp.skipped_suppressed,
        "skipped_opted_out": imp.skipped_opted_out,
    }


def _set_status(imp: ContactImport, status: str, **fields: Any) -> None:
    ContactImport.objects.filter(pk=imp.pk).update(
        status=status, updated_at=timezone.now(), **fields
    )


def _finish_unapplied(import_id: Any, status: str, error: str) -> ContactImport:
    with transaction.atomic():
        imp = ContactImport.objects.select_for_update().get(pk=import_id)
        imp.status = status
        imp.error = error[:2000]
        imp.completed_at = timezone.now()
        imp.save()
        AuditEvent.record(f"contact_import.{status.lower()}", target=imp, actor_label="system",
                          after={"error": imp.error})  # fmt: skip
    return imp


# ─── Parsing ─────────────────────────────────────────────────────────────


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportRejected(
            f"The file is not UTF-8 (byte {exc.start}). Save it as CSV UTF-8 and upload again."
        ) from exc


def _header_name(raw: str) -> str:
    name = re.sub(r"[\s\-]+", "_", raw.strip().lower())
    return _ALIASES.get(name, name)


def _reader(text: str) -> tuple[list[str], Any]:
    sample = text[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ImportRejected("The file has no header row.") from exc
    except csv.Error as exc:
        raise ImportRejected(f"The header row is not valid CSV: {exc}") from exc
    names = [_header_name(h) for h in header]
    if "email" not in names:
        raise ImportRejected('No "email" column in the header row.')
    seen: set[str] = set()
    for raw, name in zip(header, names, strict=True):
        if not _HEADER.match(name):
            raise ImportRejected(
                f"Column {raw!r} has an unusable name; use letters, digits, spaces or _."
            )
        if name in seen:
            raise ImportRejected(f"Column {name!r} appears twice in the header row.")
        seen.add(name)
    return names, reader


def _consent_time(value: str) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        day = parse_date(value)
        if day is None:
            return None
        parsed = datetime.combine(day, time.min)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _parse(content: bytes, tally: _Tally) -> list[_Row]:
    names, reader = _reader(_decode(content))
    now = timezone.now()
    rows: list[_Row] = []
    seen: set[str] = set()
    try:
        for record in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in record):
                continue  # blank line
            tally.total += 1
            if tally.total > settings.CONTACT_IMPORT_MAX_ROWS:
                raise ImportRejected(
                    f"More than {settings.CONTACT_IMPORT_MAX_ROWS:,} rows "
                    "(CONTACT_IMPORT_MAX_ROWS). Split the file."
                )
            if len(record) != len(names):
                tally.invalid += 1
                tally.issue(line, "WRONG_COLUMN_COUNT",
                            f"{len(record)} values for {len(names)} columns.")  # fmt: skip
                continue
            values = {n: v.strip() for n, v in zip(names, record, strict=True)}
            row = _row(line, values, now, tally)
            if row is None:
                continue
            if row.hashed in seen:
                tally.duplicate += 1
                tally.issue(line, "DUPLICATE_IN_FILE", "Same address as an earlier row.")
                continue
            seen.add(row.hashed)
            rows.append(row)
    except csv.Error as exc:
        raise ImportRejected(f"Not valid CSV near line {reader.line_num}: {exc}") from exc
    if tally.total == 0:
        raise ImportRejected("The file has a header but no rows.")
    return rows


def _row(line: int, values: dict[str, str], now: datetime, tally: _Tally) -> _Row | None:
    raw = values.pop("email")
    try:
        address = normalize_email(raw)
        validate_email(address)
    except (InvalidEmailError, ValidationError):
        tally.invalid += 1
        tally.issue(line, "INVALID_EMAIL", "Not a valid email address.")
        return None
    first, last = values.pop("first_name", ""), values.pop("last_name", "")
    if len(first) > _NAME_MAX or len(last) > _NAME_MAX:
        tally.invalid += 1
        tally.issue(line, "NAME_TOO_LONG", f"Names are limited to {_NAME_MAX} characters.")
        return None
    consent_raw = values.pop("consent_date", "")
    consent_at = None
    if consent_raw:
        consent_at = _consent_time(consent_raw)
        if consent_at is None or consent_at > now:
            tally.invalid += 1
            tally.issue(line, "BAD_CONSENT_DATE",
                        "consent_date must be an ISO date or datetime, not in the future.")  # fmt: skip
            return None
    return _Row(
        line=line,
        email=address,
        hashed=email_hash(address),
        first_name=first,
        last_name=last,
        consent_at=consent_at,
        attributes={k: v for k, v in values.items() if v},
    )


# ─── Applying ────────────────────────────────────────────────────────────


def _apply(imp: ContactImport, rows: list[_Row], tally: _Tally) -> None:
    lst = imp.contact_list
    for start in range(0, len(rows), _BATCH):
        _apply_batch(imp, lst, rows[start : start + _BATCH], tally)


def _apply_batch(imp: ContactImport, lst: ContactList, rows: list[_Row], tally: _Tally) -> None:
    hashes = [r.hashed for r in rows]
    blocked = Suppression.objects.blocked_among(
        [(r.hashed, r.email.rpartition("@")[2]) for r in rows], lst.stream, lst
    )
    erased = set(
        Contact.objects.filter(email_hash__in=hashes, deleted_at__isnull=False)
        .values_list("email_hash", flat=True)
    )  # fmt: skip
    existing = {c.email_hash: c for c in Contact.objects.live().filter(email_hash__in=hashes)}
    members = set(
        ListMembership.objects.filter(contact_list=lst, contact__in=existing.values())
        .values_list("contact_id", flat=True)
    )  # fmt: skip
    withdrawn: set[Any] = set()
    if lst.stream == Stream.MARKETING and existing:
        verdicts = marketing_consent_bulk([c.pk for c in existing.values()], lst)
        withdrawn = {cid for cid, v in verdicts.items() if v.code == CONSENT_WITHDRAWN}

    accepted: list[tuple[_Row, Contact | None]] = []
    for row in rows:
        contact = existing.get(row.hashed)
        if row.hashed in blocked:
            tally.suppressed += 1
            tally.issue(row.line, "SUPPRESSED", "Address or domain is suppressed for this stream.")
        elif contact is not None and contact.pk in members:
            tally.duplicate += 1
            tally.issue(row.line, "ALREADY_ON_LIST", "Already a member of this list.")
        elif contact is not None and contact.pk in withdrawn:
            tally.opted_out += 1
            tally.issue(row.line, "CONSENT_WITHDRAWN",
                        "This person withdrew marketing consent; a file cannot re-subscribe them.")  # fmt: skip
        elif contact is None and row.hashed in erased:
            tally.opted_out += 1
            tally.issue(row.line, "ERASED", "This person's data was erased; not re-added.")
        else:
            accepted.append((row, contact))

    new = [
        Contact(email=row.email, email_hash=row.hashed, first_name=row.first_name,
                last_name=row.last_name, attributes=row.attributes)  # fmt: skip
        for row, contact in accepted
        if contact is None
    ]
    created = iter(Contact.objects.bulk_create(new, batch_size=_BATCH))
    resolved = [(row, contact or next(created)) for row, contact in accepted]

    ListMembership.objects.bulk_create(
        [ListMembership(contact=c, contact_list=lst, source_import=imp) for _, c in resolved],
        batch_size=_BATCH,
    )
    if lst.stream == Stream.MARKETING:
        file_sha = imp.file_object_key.rpartition(":")[2]
        ConsentRecord.objects.bulk_create(
            [
                ConsentRecord(
                    contact=c, email_hash=c.email_hash, stream=Stream.MARKETING,
                    contact_list=lst, action=ConsentRecord.Action.GRANTED,
                    method=ConsentRecord.Method.IMPORT,
                    occurred_at=row.consent_at or imp.attested_at,
                    source=imp.consent_source[:512], source_import=imp,
                    recorded_by=imp.attested_by,
                    # The attestation itself lives once, on the import (source_import).
                    evidence={"import_id": str(imp.pk), "row": row.line, "file_sha256": file_sha,
                              "consent_date_from_file": row.consent_at is not None},
                )  # fmt: skip
                for row, c in resolved
            ],
            batch_size=_BATCH,
        )
    tally.imported += len(resolved)
