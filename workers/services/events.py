"""
Event pipeline (BUILD.md §10.8): parse → normalize → record (idempotent) → act.

Three inputs, one output. Postfix log lines, DSN/ARF emails and provider
webhooks all become DeliveryEvent rows through ``record()``, which is
idempotent on ``source_key`` and applies the event's effect in the same
transaction:

  DELIVERED / DEFERRED   counted only. A deferral after the MTA accepted the
                         message is Postfix's to retry; the job is not moved
                         back (that would make the app resend).
  HARD_BOUNCE            job ACCEPTED → BOUNCED, and a suppression:
                         5.1.x (no such mailbox/address)  → GLOBAL, permanent
                         any other 5xx (often a policy or reputation block) →
                         this stream only, so it can be lifted (Q-EVT-3:
                         suppress both, but not permanently for our mistakes)
  SOFT_BOUNCE            the message finally failed (expired / 4.x failure):
                         job → BOUNCED, counted, never suppressed (Q-EVT-1)
  COMPLAINT              marketing suppression immediately (§10.8.2)
  UNSUBSCRIBE            marketing suppression + consent withdrawal
  UNKNOWN                stored for operator review; no effect

Trust: Postfix's own log and signed webhooks act directly. A bounce or
complaint *email* acts only when it carries our signed VERP address (or, for a
complaint, one of our Message-IDs); otherwise it is UNKNOWN for review, so a
forged report cannot suppress a real customer.

Events store an email hash and the recipient domain, never the address; raw
documents (which do contain addresses) live in RawPayload, which can be
redacted. Diagnostics are stored with the address replaced.
"""

from __future__ import annotations

import email
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy as email_policy
from email.message import EmailMessage
from email.message import Message as MessageBase
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.models import AuditEvent
from common.email import InvalidEmailError, email_hash, normalize_email
from common.models import Stream
from consent.evaluation import CONSENT_WITHDRAWN, marketing_consent
from consent.models import ConsentRecord
from contacts.models import Contact
from delivery.models import Message, RecipientJob
from events.models import (
    CampaignDeliveryStats,
    DeliveryEvent,
    EventReview,
    EventSource,
    EventType,
    RawPayload,
)
from suppression.models import Suppression
from workers.services import delivery as pipeline
from workers.services.verp import parse_verp

log = logging.getLogger(__name__)
T = EventType

# §14 "Provider": group recipient domains by mailbox provider.
_PROVIDERS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "microsoft", "hotmail.com": "microsoft", "live.com": "microsoft", "msn.com": "microsoft",
    "yahoo.com": "yahoo", "ymail.com": "yahoo", "aol.com": "yahoo",
    "icloud.com": "apple", "me.com": "apple", "mac.com": "apple",
}  # fmt: skip


def provider_for(domain: str) -> str:
    return _PROVIDERS.get(domain.lower(), "other") if domain else ""


def _key(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _redact(text: str, address: str) -> str:
    return text.replace(address, "<recipient>") if address else text


# ─── Normalized event ────────────────────────────────────────────────────


@dataclass
class Normalized:
    type: str
    source: str
    source_key: str
    occurred_at: datetime
    recipient: str = ""  # used for hashing and suppression; never stored
    smtp_code: int | None = None
    enhanced_code: str = ""
    diagnostic: str = ""
    job: RecipientJob | None = None
    raw_payload: RawPayload | None = None
    final: bool = field(default=True)  # SOFT_BOUNCE: the message finally failed


@dataclass
class Outcome:
    event: DeliveryEvent
    created: bool


def _hash_and_domain(n: Normalized) -> tuple[str, str]:
    if n.recipient:
        try:
            normalized = normalize_email(n.recipient)
            return email_hash(normalized), normalized.rpartition("@")[2]
        except InvalidEmailError:
            pass
    if n.job is not None:
        return n.job.email_hash, n.job.recipient.rpartition("@")[2]
    return "", ""


def record(n: Normalized) -> Outcome:
    """Store one event exactly once and apply its effect atomically."""
    hashed, domain = _hash_and_domain(n)
    existing = DeliveryEvent.objects.filter(source_key=n.source_key).first()
    if existing is not None:
        return Outcome(existing, False)
    try:
        with transaction.atomic():
            suppression = _apply(n, hashed)
            event = DeliveryEvent.objects.create(
                type=n.type, source=n.source, source_key=n.source_key, occurred_at=n.occurred_at,
                job=n.job, campaign_id=n.job.campaign_id if n.job else None,
                email_hash=hashed, recipient_domain=domain, provider=provider_for(domain),
                smtp_code=n.smtp_code, enhanced_code=n.enhanced_code,
                diagnostic=_redact(n.diagnostic, n.recipient)[:2000],
                raw_payload=n.raw_payload, suppression=suppression,
            )  # fmt: skip
    except IntegrityError:
        # A concurrent ingest of the same report won the race.
        return Outcome(DeliveryEvent.objects.get(source_key=n.source_key), False)
    if n.type in (T.HARD_BOUNCE, T.COMPLAINT, T.UNKNOWN):
        log.info("delivery event", extra={"type": n.type, "event_id": str(event.pk),
                                          "job": str(n.job.pk) if n.job else None})  # fmt: skip
    return Outcome(event, True)


def _bounce_job(job: RecipientJob | None, code: str, detail: str) -> None:
    if job is None:
        return
    locked = pipeline._locked(job.pk)
    if locked.state == RecipientJob.State.ACCEPTED:
        pipeline._transition(locked, RecipientJob.State.BOUNCED, code=code, detail=detail[:500])
        locked.save()


def _apply(n: Normalized, hashed: str) -> Suppression | None:
    source = f"{n.source.lower()}:{n.enhanced_code or n.smtp_code or ''}".rstrip(":")
    if n.type == T.HARD_BOUNCE:
        _bounce_job(n.job, f"BOUNCE_{n.enhanced_code or n.smtp_code}", n.diagnostic)
        if not hashed:
            return None
        if n.enhanced_code.startswith("5.1."):
            row, _ = Suppression.suppress_hash(
                hashed, Suppression.Reason.HARD_BOUNCE, source=source
            )
        else:
            stream = n.job.purpose if n.job else Stream.MARKETING
            row, _ = Suppression.suppress_hash(hashed, Suppression.Reason.HARD_BOUNCE,
                                               scope=Suppression.Scope.STREAM, stream=stream,
                                               source=source)  # fmt: skip
        return row
    if n.type == T.SOFT_BOUNCE:
        if n.final:
            _bounce_job(n.job, f"FAILED_{n.enhanced_code or 'SOFT'}", n.diagnostic)
        return None  # Q-EVT-1: count only
    if n.type == T.COMPLAINT and hashed:
        row, _ = Suppression.suppress_hash(hashed, Suppression.Reason.COMPLAINT,
                                           scope=Suppression.Scope.STREAM, stream=Stream.MARKETING,
                                           source=source)  # fmt: skip
        return row
    if n.type == T.UNSUBSCRIBE and hashed:
        row, _ = Suppression.suppress_hash(hashed, Suppression.Reason.UNSUBSCRIBE,
                                           scope=Suppression.Scope.STREAM, stream=Stream.MARKETING,
                                           source=source)  # fmt: skip
        contact = Contact.objects.live().filter(email_hash=hashed).first()
        if contact and marketing_consent(contact.pk).code != CONSENT_WITHDRAWN:
            ConsentRecord.objects.create(contact=contact, stream=Stream.MARKETING,
                                         action=ConsentRecord.Action.WITHDRAWN,
                                         method=ConsentRecord.Method.LIST_UNSUBSCRIBE,
                                         source=source)  # fmt: skip
        return row
    return None


def _job_for_message_id(message_id: str) -> RecipientJob | None:
    if not message_id:
        return None
    msg = Message.objects.select_related("job").filter(message_id=message_id.strip()).first()
    return msg.job if msg else None


# ─── Postfix delivery log ────────────────────────────────────────────────

_POSTFIX = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d\d:\d\d:\d\d|\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)"
    r"\s+\S+\s+postfix[\w.-]*/[\w/-]+\[\d+\]:\s+(?P<qid>[0-9A-Za-z]+):\s+"
    r"to=<(?P<to>[^>]*)>,.*?\bdsn=(?P<dsn>\d\.\d{1,3}\.\d{1,3}),\s+"
    r"status=(?P<status>sent|deferred|bounced|expired)\s*(?:\((?P<resp>.*)\))?\s*$"
)
_REPLY_CODE = re.compile(r"(?:said:\s*|^)([245]\d\d)[\s-]")


def _syslog_time(text: str, now: datetime) -> datetime:
    if text[0].isdigit():
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    parsed = datetime.strptime(f"{now.year} {' '.join(text.split())}", "%Y %b %d %H:%M:%S")
    parsed = parsed.replace(tzinfo=UTC)
    if parsed > now + timedelta(days=1):  # a December line read in January
        parsed = parsed.replace(year=now.year - 1)
    return parsed


def parse_postfix_line(line: str, *, now: datetime | None = None) -> Normalized | None:
    """One delivery-status line, or None if the line is not one."""
    m = _POSTFIX.match(line.strip())
    if not m:
        return None
    now = now or timezone.now()
    status, dsn, to = m["status"], m["dsn"], m["to"]
    resp = m["resp"] or ""
    code_match = _REPLY_CODE.search(resp)
    if status == "sent":
        kind = T.DELIVERED
    elif status == "deferred":
        kind = T.DEFERRED
    elif dsn.startswith("5"):
        kind = T.HARD_BOUNCE
    else:  # bounced with 4.x.x, or expired after the queue lifetime
        kind = T.SOFT_BOUNCE
    job = None
    msg = Message.objects.select_related("job").filter(mta_queue_id=m["qid"]).first()
    if msg is not None and to:
        try:
            if email_hash(to) == msg.job.email_hash:  # queue ids are reused eventually
                job = msg.job
        except InvalidEmailError:
            pass
    return Normalized(
        type=kind, source=EventSource.POSTFIX_LOG,
        source_key=_key("postfix", m["qid"], to.lower(), status, dsn, m["ts"]),
        occurred_at=_syslog_time(m["ts"], now), recipient=to,
        smtp_code=int(code_match.group(1)) if code_match else None, enhanced_code=dsn,
        diagnostic=f"status={status} {resp}".strip(), job=job,
    )  # fmt: skip


def ingest_log_lines(lines: list[str]) -> dict[str, int]:
    counts = {"events": 0, "duplicates": 0, "ignored": 0}
    for line in lines:
        n = parse_postfix_line(line)
        if n is None:
            counts["ignored"] += 1
            continue
        counts["events" if record(n).created else "duplicates"] += 1
    return counts


# ─── DSN (RFC 3464) and ARF (RFC 5965) emails ────────────────────────────


def _store_raw(source: str, external_id: str, body: str) -> tuple[RawPayload, bool]:
    return RawPayload.objects.get_or_create(source=source, external_id=external_id[:255],
                                            defaults={"body": body})  # fmt: skip


def _verp_job(*addresses: str | None) -> RecipientJob | None:
    for address in addresses:
        if address:
            for part in re.split(r"[,\s]+", address):
                job_id = parse_verp(part.strip("<>"))
                if job_id is not None:
                    return RecipientJob.objects.filter(pk=job_id).first()
    return None


def _status_code(text: str) -> str:
    m = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", text or "")
    return m.group(1) if m else ""


def _reply_code(text: str) -> int | None:
    m = re.search(r"\b([245]\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def ingest_mail(raw: bytes) -> list[Outcome]:
    """A message delivered to the bounce or FBL mailbox (piped from Postfix)."""
    msg = email.message_from_bytes(raw, policy=email_policy.default)
    assert isinstance(msg, EmailMessage)
    external_id = (msg.get("Message-ID") or "").strip() or _key("mail", raw)
    report_type = (
        str(msg.get_param("report-type") or "").lower()
        if msg.get_content_type() == "multipart/report"
        else ""
    )
    source = EventSource.FBL if report_type == "feedback-report" else EventSource.DSN
    payload, _ = _store_raw(source, external_id, raw.decode("utf-8", "replace"))
    envelope = _verp_job(msg.get("X-Original-To"), msg.get("Delivered-To"), msg.get("To"))
    when = msg.get("Date")
    occurred = (
        when.datetime if when is not None and getattr(when, "datetime", None) else timezone.now()
    )

    if report_type == "delivery-status":
        outcomes = _ingest_dsn(msg, payload, external_id, envelope, occurred)
    elif report_type == "feedback-report":
        outcomes = _ingest_arf(msg, payload, external_id, envelope, occurred)
    else:
        outcomes = []
    if not outcomes:
        outcomes = [record(Normalized(
            type=T.UNKNOWN, source=source, source_key=_key("unknown", external_id), occurred_at=occurred,
            diagnostic=f"unrecognised {msg.get_content_type()} report-type={report_type or '-'}: "
                       f"{msg.get('Subject', '')}"[:500],
            job=envelope, raw_payload=payload,
        ))]  # fmt: skip
    if payload.processed_at is None:
        RawPayload.objects.filter(pk=payload.pk).update(processed_at=timezone.now())
    return outcomes


def _ingest_dsn(msg: EmailMessage, payload: RawPayload, external_id: str,
                job: RecipientJob | None, occurred: datetime) -> list[Outcome]:  # fmt: skip
    status_part = next(
        (p for p in msg.walk() if p.get_content_type() == "message/delivery-status"), None
    )
    if status_part is None:
        return []
    blocks = status_part.get_payload()
    if not isinstance(blocks, list):
        return []
    outcomes = []
    for block in blocks[1:]:  # [0] is the per-message block
        if not isinstance(block, MessageBase):
            continue
        recipient = str(block.get("Final-Recipient") or block.get("Original-Recipient") or "")
        recipient = recipient.split(";", 1)[-1].strip()
        action = str(block.get("Action") or "").strip().lower()
        status = str(block.get("Status") or "").strip()
        diagnostic = str(block.get("Diagnostic-Code") or "")
        if not action or not status:
            continue
        if action == "failed":
            kind = T.HARD_BOUNCE if status.startswith("5") else T.SOFT_BOUNCE
        elif action == "delayed":
            kind = T.DEFERRED
        elif action in ("delivered", "relayed", "expanded"):
            kind = T.DELIVERED
        else:
            kind = T.UNKNOWN
        target_job = job if job is not None and (not recipient or _same(job, recipient)) else None
        if target_job is None and kind != T.UNKNOWN:
            # Only a bounce addressed to a signed VERP address is trusted to act.
            # Anyone can email the bounce domain; a forged DSN naming a real
            # customer must not suppress them. It waits for an operator instead.
            kind = T.UNKNOWN
        outcomes.append(record(Normalized(
            type=kind, source=EventSource.DSN,
            source_key=_key("dsn", external_id, recipient.lower(), action, status),
            occurred_at=occurred, recipient=recipient, smtp_code=_reply_code(diagnostic),
            enhanced_code=_status_code(status) or status[:16], diagnostic=f"{action}; {diagnostic}".strip(),
            job=target_job, raw_payload=payload,
        )))  # fmt: skip
    return outcomes


def _same(job: RecipientJob, recipient: str) -> bool:
    try:
        return email_hash(recipient) == job.email_hash
    except InvalidEmailError:
        return False


def _ingest_arf(msg: EmailMessage, payload: RawPayload, external_id: str,
                job: RecipientJob | None, occurred: datetime) -> list[Outcome]:  # fmt: skip
    report = next(
        (p for p in msg.walk() if p.get_content_type() == "message/feedback-report"), None
    )
    if report is None:
        return []
    # The email package parses a message/* body as a header block.
    blocks = report.get_payload()
    fields = blocks[0] if isinstance(blocks, list) and blocks else None
    feedback_type = (
        str(fields.get("Feedback-Type", "")).strip().lower()
        if isinstance(fields, MessageBase)
        else ""
    )
    kind = T.COMPLAINT if feedback_type in ("abuse", "fraud") else T.UNKNOWN

    # The complained-about message: its headers carry our VERP Return-Path,
    # our Message-ID and the recipient.
    original = next((p for p in msg.walk()
                     if p.get_content_type() in ("message/rfc822", "text/rfc822-headers")), None)  # fmt: skip
    headers: MessageBase | None = None
    if original is not None and original.get_content_type() == "message/rfc822":
        inner = original.get_payload()
        first = inner[0] if isinstance(inner, list) and inner else None
        headers = first if isinstance(first, MessageBase) else None
    elif isinstance(original, EmailMessage):
        headers = email.message_from_string(original.get_content(), policy=email_policy.default)
    return_path = str(headers.get("Return-Path", "")) if headers is not None else ""
    message_id = str(headers.get("Message-ID", "")) if headers is not None else ""
    to_header = headers.get("To") if headers is not None else None
    addresses = getattr(to_header, "addresses", ()) if to_header is not None else ()
    recipient = addresses[0].addr_spec if addresses else ""

    target = job or _verp_job(return_path) or _job_for_message_id(message_id)
    if target is None:
        # As for DSNs: without our signed Return-Path or one of our (unguessable)
        # Message-IDs, a complaint could be forged to suppress anyone.
        kind = T.UNKNOWN
    return [record(Normalized(
        type=kind, source=EventSource.FBL, source_key=_key("arf", external_id),
        occurred_at=occurred, recipient=recipient, job=target, raw_payload=payload,
        diagnostic=f"feedback-type={feedback_type or '-'}",
    ))]  # fmt: skip


# ─── Provider webhooks ───────────────────────────────────────────────────

_WEBHOOK_TYPES = {
    "delivered": T.DELIVERED, "deferred": T.DEFERRED, "complaint": T.COMPLAINT,
    "unsubscribe": T.UNSUBSCRIBE, "hard_bounce": T.HARD_BOUNCE, "soft_bounce": T.SOFT_BOUNCE,
}  # fmt: skip


def normalize_webhook(payload: RawPayload) -> Outcome:
    data = json.loads(payload.body)
    kind_name = str(data.get("type", "")).lower()
    enhanced = str(data.get("enhanced_code") or "")
    if kind_name == "bounce":
        kind = T.HARD_BOUNCE if enhanced.startswith("5") or str(data.get("smtp_code", "")).startswith("5") \
            else T.SOFT_BOUNCE if enhanced.startswith("4") else T.UNKNOWN  # fmt: skip
    else:
        kind = _WEBHOOK_TYPES.get(kind_name, T.UNKNOWN)
    occurred = parse_datetime(str(data.get("occurred_at") or "")) or timezone.now()
    if timezone.is_naive(occurred):
        occurred = occurred.replace(tzinfo=UTC)
    smtp_code = data.get("smtp_code")
    outcome = record(Normalized(
        type=kind, source=EventSource.WEBHOOK, source_key=_key("webhook", payload.external_id),
        occurred_at=occurred, recipient=str(data.get("recipient") or ""),
        smtp_code=int(smtp_code) if str(smtp_code or "").isdigit() else None, enhanced_code=enhanced[:16],
        diagnostic=str(data.get("diagnostic") or "")[:1000],
        job=_job_for_message_id(str(data.get("message_id") or "")), raw_payload=payload,
    ))  # fmt: skip
    RawPayload.objects.filter(pk=payload.pk).update(processed_at=timezone.now())
    return outcome


# ─── Operator review of UNKNOWN events ───────────────────────────────────


def resolve_unknown(
    event: DeliveryEvent, resolution: str, *, actor: Any, note: str = ""
) -> EventReview:
    if event.type != T.UNKNOWN:
        raise ValueError("only unclassified events are reviewed")
    if resolution not in EventReview.Resolution.values:
        raise ValueError(f"unknown resolution {resolution!r}")
    with transaction.atomic():
        review = EventReview.objects.create(
            event=event, resolution=resolution, actor=actor, note=note
        )
        if resolution != EventReview.Resolution.IGNORE:
            # The event stores only a hash; suppression and job effects work from that.
            n = Normalized(type=resolution, source=EventSource.OPERATOR,
                           source_key=_key("review", event.pk), occurred_at=event.occurred_at,
                           enhanced_code=event.enhanced_code, smtp_code=event.smtp_code,
                           diagnostic=f"operator review: {note}".strip(), job=event.job,
                           raw_payload=event.raw_payload)  # fmt: skip
            _record_with_hash(n, event.email_hash, event.recipient_domain)
        AuditEvent.record(
            "event.review", actor=actor, target=event, resolution=resolution, note=note
        )
    return review


def _record_with_hash(n: Normalized, hashed: str, domain: str) -> DeliveryEvent:
    suppression = _apply(n, hashed)
    return DeliveryEvent.objects.create(
        type=n.type, source=n.source, source_key=n.source_key, occurred_at=n.occurred_at, job=n.job,
        campaign_id=n.job.campaign_id if n.job else None, email_hash=hashed, recipient_domain=domain,
        provider=provider_for(domain), smtp_code=n.smtp_code, enhanced_code=n.enhanced_code,
        diagnostic=n.diagnostic[:2000], raw_payload=n.raw_payload, suppression=suppression,
    )  # fmt: skip


def record_unsubscribe_link(contact_id: Any, campaign_id: Any, hashed: str, domain: str) -> None:
    """Called by the unsubscribe page so link unsubscribes appear in telemetry.
    The token's campaign id is only trusted if it names a real campaign."""
    from campaigns.models import Campaign

    key = _key("unsub-link", contact_id, campaign_id or "-")
    try:
        campaign_id = Campaign.objects.filter(pk=campaign_id).values_list("pk", flat=True).first()
    except (ValueError, ValidationError):
        campaign_id = None
    if DeliveryEvent.objects.filter(source_key=key).exists():
        return
    try:
        with transaction.atomic():
            DeliveryEvent.objects.create(
                type=T.UNSUBSCRIBE, source=EventSource.UNSUBSCRIBE_LINK, source_key=key,
                occurred_at=timezone.now(), campaign_id=campaign_id or None, email_hash=hashed,
                recipient_domain=domain, provider=provider_for(domain),
            )  # fmt: skip
    except IntegrityError:
        pass  # two clicks at once; one event is enough


# ─── Aggregation (§10.8.3) and alerts (Q-EVT-2: alert only) ──────────────

_STAT_FIELDS: dict[str, str] = {
    T.DELIVERED: "delivered", T.DEFERRED: "deferred", T.HARD_BOUNCE: "hard_bounces",
    T.SOFT_BOUNCE: "soft_bounces", T.COMPLAINT: "complaints", T.UNSUBSCRIBE: "unsubscribes",
    T.UNKNOWN: "unknown",
}  # fmt: skip


def recompute_campaign_stats(campaign_ids: list[Any] | None = None) -> int:
    qs = DeliveryEvent.objects.exclude(campaign__isnull=True)
    if campaign_ids is not None:
        qs = qs.filter(campaign_id__in=campaign_ids)
    totals: dict[Any, dict[str, int]] = {}
    for row in qs.values("campaign_id", "type").annotate(n=Count("pk")):
        totals.setdefault(row["campaign_id"], {})[_STAT_FIELDS[row["type"]]] = row["n"]
    for campaign_id, counts in totals.items():
        CampaignDeliveryStats.objects.update_or_create(
            campaign_id=campaign_id, defaults={f: counts.get(f, 0) for f in _STAT_FIELDS.values()}
        )
    return len(totals)


def rate_alerts() -> list[dict[str, Any]]:
    """Campaigns over the configured complaint / hard-bounce rate. Alerts only."""
    out = []
    for s in CampaignDeliveryStats.objects.select_related("campaign"):
        base = s.delivered + s.hard_bounces + s.soft_bounces
        if not base:
            continue
        for label, count, limit in (
            ("complaint", s.complaints, settings.COMPLAINT_RATE_ALERT_THRESHOLD),
            ("hard-bounce", s.hard_bounces, settings.HARD_BOUNCE_RATE_ALERT_THRESHOLD),
        ):
            rate = count / base
            if count and rate >= limit:
                out.append({"campaign": s.campaign, "kind": label, "rate": rate, "limit": limit,
                            "count": count, "base": base})  # fmt: skip
    return out
