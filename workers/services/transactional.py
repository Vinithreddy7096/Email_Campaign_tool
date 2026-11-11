"""
Transactional sends (BUILD.md §10.6.6): one message, one RecipientJob.

Checked when the request arrives, so the caller gets an answer it can act on:
  template   exists, is transactional, has an approved version (newest is used)
  sender     an active SenderIdentity on a transactional sending domain
  recipient  a valid address
  data       a JSON object that renders the template without missing variables

Checked again at hand-off by the policy service, exactly as for campaigns:
suppression (Q-POL-2 default: every suppression applies to transactional mail),
pauses — including pausing this API client — and domain status. A message the
policy holds or suppresses was still accepted here; the response says so.

Idempotency (§5.2, §10.7.1): the job key is SHA-256 of the caller's key and the
API client (the token's user, so it survives token rotation). The same key and
body returns the original message; the same key with a different body is
refused, so a retry can never silently send something else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import ApiToken
from common.email import InvalidEmailError, email_hash, normalize_email
from common.models import Stream
from contacts.models import Contact
from delivery.models import RecipientJob, RecipientJobTransition
from domains.models import SenderIdentity, SendingDomain
from email_templates.models import EmailTemplate
from email_templates.rendering import RenderError, render
from suppression.models import Suppression

State = RecipientJob.State
MAX_DATA_BYTES = 64 * 1024
MAX_KEY_LENGTH = 200


class MessageRejected(Exception):
    def __init__(self, code: str, detail: str, field_name: str = ""):
        self.code, self.detail, self.field = code, detail, field_name
        super().__init__(detail)


class IdempotencyConflict(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate limit reached; retry in {retry_after}s")


@dataclass
class Accepted:
    job: RecipientJob
    created: bool
    warnings: list[str] = field(default_factory=list)


def job_key(client_key: str, client_id: Any) -> str:
    return hashlib.sha256(f"txn|{client_id}|{client_key}".encode()).hexdigest()


def _fingerprint(template: str, sender: str, to: str, data: dict[str, Any]) -> str:
    canonical = json.dumps({"template": template, "sender": sender, "to": to, "data": data},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


def _rate_limit(token: ApiToken) -> None:
    """Q-POL-1: no default number. 0 means no limit is applied."""
    limit = settings.MESSAGES_API_RATE_PER_MINUTE
    if limit <= 0:
        return
    now = int(timezone.now().timestamp())
    window = f"msgrate:{token.pk}:{now // 60}"
    cache.add(window, 0, timeout=120)
    if cache.incr(window) > limit:
        raise RateLimited(60 - now % 60)


def _template(name: str) -> tuple[EmailTemplate, Any]:
    template = EmailTemplate.objects.filter(name=name, archived_at__isnull=True).first()
    if template is None:
        raise MessageRejected("TEMPLATE_UNKNOWN", f"No template named {name!r}.", "template")
    if template.category != EmailTemplate.Category.TRANSACTIONAL:
        raise MessageRejected("TEMPLATE_NOT_TRANSACTIONAL",
                              f"{name!r} is a {template.category} template; this endpoint sends "
                              "transactional mail only.", "template")  # fmt: skip
    version = template.latest_approved_version()
    if version is None:
        raise MessageRejected(
            "TEMPLATE_NOT_APPROVED", f"{name!r} has no approved version.", "template"
        )
    return template, version


def _sender(address: str) -> SenderIdentity:
    local, _, domain = address.strip().lower().rpartition("@")
    sender = (SenderIdentity.objects.select_related("domain")
              .filter(local_part=local, domain__domain=domain).first())  # fmt: skip
    if sender is None or not sender.is_active:
        raise MessageRejected(
            "SENDER_UNKNOWN", f"{address!r} is not an active sender identity.", "sender"
        )
    if sender.domain.stream != Stream.TRANSACTIONAL:
        raise MessageRejected("SENDER_NOT_TRANSACTIONAL",
                              f"{address!r} sends on a {sender.domain.stream} domain.", "sender")  # fmt: skip
    return sender


def submit_message(token: ApiToken, *, template: str, sender: str, to: str, data: Any,
                   idempotency_key: str) -> Accepted:  # fmt: skip
    if not idempotency_key or len(idempotency_key) > MAX_KEY_LENGTH:
        raise MessageRejected("IDEMPOTENCY_KEY_REQUIRED",
                              f"Send an idempotency_key of 1–{MAX_KEY_LENGTH} characters.",
                              "idempotency_key")  # fmt: skip
    if not isinstance(data, dict):
        raise MessageRejected("DATA_NOT_OBJECT", "data must be a JSON object.", "data")
    if len(json.dumps(data)) > MAX_DATA_BYTES:
        raise MessageRejected("DATA_TOO_LARGE", f"data is over {MAX_DATA_BYTES} bytes.", "data")
    try:
        recipient = normalize_email(to)
    except (InvalidEmailError, AttributeError) as exc:
        raise MessageRejected("INVALID_RECIPIENT", f"{to!r} is not a valid address.", "to") from exc

    key = job_key(idempotency_key, token.user_id)
    fingerprint = _fingerprint(template, sender.strip().lower(), recipient, data)
    existing = RecipientJob.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return _replay(existing, fingerprint)

    _rate_limit(token)
    tpl, version = _template(template)
    identity = _sender(sender)
    try:
        render(version, data, {}, require_approved=True)  # fail now, not at 3 a.m. in a worker
    except RenderError as exc:
        raise MessageRejected("RENDER_FAILED", str(exc), "data") from exc

    hashed = email_hash(recipient)
    contact = Contact.objects.live().filter(email_hash=hashed).first()
    try:
        with transaction.atomic():
            job = RecipientJob.objects.create(
                idempotency_key=key, purpose=Stream.TRANSACTIONAL, contact=contact,
                recipient=recipient, email_hash=hashed, sender_identity=identity,
                template_version=version, expected_checksum=version.checksum, from_snapshot=False,
                api_client_ref=str(token.pk), merge_data=data, request_fingerprint=fingerprint,
                state=State.QUEUED,
            )  # fmt: skip
            RecipientJobTransition.objects.bulk_create([
                RecipientJobTransition(job=job, from_state=State.DRAFT, to_state=State.ELIGIBLE,
                                       code="API_ACCEPTED", detail=f"template {tpl.name} v{version.number}"),
                RecipientJobTransition(job=job, from_state=State.ELIGIBLE, to_state=State.QUEUED,
                                       code="QUEUED_FOR_RENDER"),
            ])  # fmt: skip
            from workers.tasks.delivery import render as render_task

            transaction.on_commit(partial(render_task.delay, str(job.pk)))
    except IntegrityError:
        # The same key arrived twice at once; the other request won.
        return _replay(RecipientJob.objects.get(idempotency_key=key), fingerprint)
    return Accepted(job, True, _warnings(identity, recipient))


def _replay(job: RecipientJob, fingerprint: str) -> Accepted:
    if job.request_fingerprint != fingerprint:
        raise IdempotencyConflict("This idempotency_key was already used for a different message.")
    return Accepted(job, False)


def _warnings(sender: SenderIdentity, recipient: str) -> list[str]:
    """Accepted, but it will not go out right now — tell the caller why."""
    out = []
    if Suppression.objects.blocking_for_send(recipient, Stream.TRANSACTIONAL).exists():
        out.append("The recipient is suppressed; this message will not be sent.")
    if sender.domain.status != SendingDomain.Status.ACTIVE:
        out.append(f"Sending domain {sender.domain} is {sender.domain.status}; "
                   "the message is held until it is active.")  # fmt: skip
    if not settings.GLOBAL_SEND_ENABLED:
        out.append(
            "Sending is disabled on this instance (GLOBAL_SEND_ENABLED=false); the message is held."
        )
    return out
