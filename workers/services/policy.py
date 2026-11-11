"""
Policy service: the last decision before MTA handoff (BUILD.md §10.9).

``decide(request)`` runs checks 1–7 in the spec's order and returns SEND,
SUPPRESS or HOLD with every reason found:

1. Kill switch and pauses (global, stream/route, domain, IP pool, campaign,
   API client), plus the non-production recipient allowlist (§4.2) — HOLD.
2. Suppression of the address or its domain — SUPPRESS. Marketing honours
   GLOBAL, marketing-stream and this-list rows; transactional honours every
   active row for the address (Q-POL-2 default: all suppressions apply).
3. Marketing only: current consent, not withdrawn, not expired — SUPPRESS
   (consent.evaluation, shared with campaign preflight).
4. Sending domain ACTIVE and sender identity active — HOLD.
5. Template version APPROVED (or RETIRED after the campaign snapshot), its
   category matches the purpose, and its checksum matches the snapshot — HOLD.

6. Rate limits by instance, campaign, domain, provider, IP pool, API client
   — HOLD until there is room (workers.services.ratelimit).
7. Warm-up plan for the domain / IP pool / provider — HOLD until tomorrow.

Checks 6–7 take allowance, so they run only when 1–5 found nothing: a message
held or suppressed for another reason must not use up a limit. Their numbers
are Q-POL-1; with no RateLimit / WarmupPlan rows nothing is limited.

Fail-closed rules beyond the table:
- The outcome is that of the first failing check in spec order; every failing
  check is still recorded as a reason.
- Objects are re-read from the database: a caller holding a stale domain or
  template must not send on yesterday's state.
- An exception inside a check is a HOLD reason (POLICY_ERROR), never a SEND.
- If a SEND decision cannot be logged, it becomes HOLD (POLICY_LOG_FAILED):
  §10.9 requires every decision to be logged.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings

from common.email import InvalidEmailError, email_hash, normalize_email
from common.models import Stream
from consent import evaluation as consent_rules
from consent.evaluation import marketing_consent
from contacts.models import Contact, ContactList
from delivery.models import PolicyDecision, SendPause
from domains.models import SenderIdentity, SendingDomain
from email_templates.models import EmailTemplate, TemplateVersion
from suppression.models import Suppression
from workers.services import ratelimit

log = logging.getLogger(__name__)

Outcome = PolicyDecision.Outcome
SEND, SUPPRESS, HOLD = Outcome.SEND, Outcome.SUPPRESS, Outcome.HOLD

# email_templates uses lowercase category values; streams are uppercase.
_CATEGORY_FOR_PURPOSE: dict[str, str] = {
    Stream.MARKETING: EmailTemplate.Category.MARKETING,
    Stream.TRANSACTIONAL: EmailTemplate.Category.TRANSACTIONAL,
}


@dataclass(frozen=True)
class SendRequest:
    """Everything the policy needs about one send. RecipientJob (§10.7) builds this."""

    recipient: str
    purpose: str  # common.models.Stream
    sender: SenderIdentity
    template_version: TemplateVersion
    # The checksum frozen into the campaign snapshot, or recorded when a
    # transactional job was created.
    expected_checksum: str
    # True for campaign sends: a version RETIRED after the snapshot may finish.
    from_snapshot: bool = False
    contact: Contact | None = None
    contact_list: ContactList | None = None
    campaign_ref: str = ""
    api_client_ref: str = ""
    job_ref: str = ""


@dataclass(frozen=True)
class Reason:
    check: int
    code: str
    outcome: str
    detail: str
    retry_after_s: int = 0  # HOLD: when to ask again (0: the default hold interval)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "check": self.check,
            "code": self.code,
            "outcome": self.outcome,
            "detail": self.detail,
        }
        if self.retry_after_s:
            out["retry_after_s"] = self.retry_after_s
        return out


@dataclass(frozen=True)
class Decision:
    outcome: str
    reasons: tuple[Reason, ...] = field(default=())
    record_id: uuid.UUID | None = None

    @property
    def send(self) -> bool:
        return self.outcome == SEND

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.reasons]

    @property
    def retry_after_s(self) -> int:
        return max((r.retry_after_s for r in self.reasons), default=0)


@dataclass
class _Fresh:
    """Current database state for the objects named in a request."""

    sender: SenderIdentity
    domain: SendingDomain
    version: TemplateVersion
    contact: Contact | None
    recipient: str | None  # normalized; None if invalid


def _load(req: SendRequest) -> _Fresh:
    sender = SenderIdentity.objects.select_related("domain__ip_pool").get(pk=req.sender.pk)
    version = TemplateVersion.objects.select_related("template").get(pk=req.template_version.pk)
    contact = Contact.objects.get(pk=req.contact.pk) if req.contact is not None else None
    recipient: str | None = None
    if isinstance(req.recipient, str):
        with contextlib.suppress(InvalidEmailError):
            recipient = normalize_email(req.recipient)
    return _Fresh(sender, sender.domain, version, contact, recipient)


# ─── Checks ──────────────────────────────────────────────────────────────


def _check_pauses(req: SendRequest, f: _Fresh) -> list[Reason]:
    out: list[Reason] = []
    if not settings.GLOBAL_SEND_ENABLED:
        out.append(Reason(1, "KILL_SWITCH", HOLD, "GLOBAL_SEND_ENABLED is false"))

    environment = getattr(settings, "ENVIRONMENT", "unknown")
    if environment != "production" and not settings.MOCK_MODE and f.recipient:
        allowed = {d.lower() for d in settings.ALLOWED_TEST_RECIPIENT_DOMAINS}
        if f.recipient.rpartition("@")[2] not in allowed:
            out.append(
                Reason(1, "TEST_RECIPIENT_NOT_ALLOWED", HOLD,
                       f"{environment} may only send to ALLOWED_TEST_RECIPIENT_DOMAINS")
            )  # fmt: skip

    targets = [(SendPause.Scope.GLOBAL, ""), (SendPause.Scope.STREAM, req.purpose),
               (SendPause.Scope.DOMAIN, str(f.domain.pk))]  # fmt: skip
    if f.domain.ip_pool_id:
        targets.append((SendPause.Scope.IP_POOL, str(f.domain.ip_pool_id)))
    if req.campaign_ref:
        targets.append((SendPause.Scope.CAMPAIGN, req.campaign_ref))
    if req.api_client_ref:
        targets.append((SendPause.Scope.API_CLIENT, req.api_client_ref))
    match = None
    for scope, target in targets:
        q = SendPause.objects.active().filter(scope=scope, target=target)
        match = q if match is None else match | q
    for pause in match.order_by("created_at") if match is not None else []:
        out.append(Reason(1, "PAUSED", HOLD, f"{pause.get_scope_display()} paused: {pause.reason}"))

    # Routing is a fixed stream → pool assignment (§10.3); never pick another.
    if f.domain.stream != req.purpose:
        out.append(Reason(1, "STREAM_MISMATCH", HOLD,
                          f"{f.domain} is a {f.domain.stream} domain; this is {req.purpose} mail"))  # fmt: skip
    pool = f.domain.ip_pool
    if pool is not None and not pool.is_active:
        out.append(Reason(1, "IP_POOL_INACTIVE", HOLD, f"IP pool {pool.name} is inactive"))
    if pool is None and not settings.MOCK_MODE:
        # In MOCK_MODE all mail goes to the sink and routing is ignored (§5.3).
        out.append(Reason(1, "NO_ROUTE", HOLD, f"{f.domain} has no IP pool"))
    return out


def _check_suppression(req: SendRequest, f: _Fresh) -> list[Reason]:
    if f.recipient is None:
        return [Reason(2, "INVALID_ADDRESS", SUPPRESS, "recipient is not a valid address")]
    out: list[Reason] = []
    if f.contact is not None and f.contact.deleted_at is not None:
        out.append(Reason(2, "CONTACT_ERASED", SUPPRESS, "contact has been erased"))
    rows = Suppression.objects.blocking_for_send(f.recipient, req.purpose, req.contact_list)
    for row in rows.order_by("created_at"):
        target = f"@{row.domain}" if row.domain else "address"
        out.append(Reason(2, "SUPPRESSED", SUPPRESS,
                          f"{row.get_scope_display()} {row.get_reason_display()} ({target})"))  # fmt: skip
    return out


def _check_consent(req: SendRequest, f: _Fresh) -> list[Reason]:
    if req.purpose != Stream.MARKETING:
        return []
    if f.contact is None:
        return [Reason(3, "NO_CONSENT", SUPPRESS, "marketing needs a known contact with consent")]
    verdict = marketing_consent(f.contact.pk, req.contact_list)  # same rule as campaign preflight
    if verdict.granted:
        return []
    detail = {
        consent_rules.CONSENT_WITHDRAWN: "marketing consent was withdrawn",
        consent_rules.CONSENT_EXPIRED:
            f"consent is older than CONSENT_MAX_AGE_DAYS={settings.CONSENT_MAX_AGE_DAYS}",
        consent_rules.NO_CONSENT: "no marketing consent on record",
    }[verdict.code]  # fmt: skip
    return [Reason(3, verdict.code, SUPPRESS, detail)]


def _check_domain(req: SendRequest, f: _Fresh) -> list[Reason]:
    out: list[Reason] = []
    if f.domain.status != SendingDomain.Status.ACTIVE:
        out.append(Reason(4, "DOMAIN_NOT_ACTIVE", HOLD, f"{f.domain} is {f.domain.status}"))
    if not f.sender.is_active:
        out.append(
            Reason(4, "SENDER_INACTIVE", HOLD, f"sender {f.sender.from_address} is inactive")
        )
    return out


def _check_template(req: SendRequest, f: _Fresh) -> list[Reason]:
    v = f.version
    out: list[Reason] = []
    status_ok = v.status == TemplateVersion.Status.APPROVED or (
        v.status == TemplateVersion.Status.RETIRED and req.from_snapshot
    )
    if not status_ok:
        out.append(Reason(5, "TEMPLATE_NOT_APPROVED", HOLD, f"{v} is {v.status}"))
    if v.template.category != _CATEGORY_FOR_PURPOSE.get(req.purpose):
        # A transactional template carries no unsubscribe link; it must never
        # go out as marketing, and vice versa.
        out.append(Reason(5, "TEMPLATE_PURPOSE_MISMATCH", HOLD,
                          f"{v} is a {v.template.category} template; this is {req.purpose} mail"))  # fmt: skip
    if v.checksum != req.expected_checksum:
        out.append(Reason(5, "TEMPLATE_CHECKSUM_MISMATCH", HOLD,
                          "template content differs from the approved snapshot"))  # fmt: skip
    if v.compute_checksum() != v.checksum:
        out.append(Reason(5, "TEMPLATE_TAMPERED", HOLD,
                          "stored checksum does not match the template content"))  # fmt: skip
    return out


CHECKS: tuple[tuple[int, Callable[[SendRequest, _Fresh], list[Reason]]], ...] = (
    (1, _check_pauses),
    (2, _check_suppression),
    (3, _check_consent),
    (4, _check_domain),
    (5, _check_template),
)


def _check_limits(req: SendRequest, f: _Fresh, now: datetime | None) -> list[Reason]:
    route = ratelimit.Route(
        domain_id=str(f.domain.pk), pool_id=str(f.domain.ip_pool_id or ""),
        campaign_ref=req.campaign_ref, api_client_ref=req.api_client_ref,
        recipient_domain=(f.recipient or "").rpartition("@")[2],
    )  # fmt: skip
    return [Reason(r.check, r.code, HOLD, r.detail, r.retry_after_s)
            for r in ratelimit.acquire(route, now=now)]  # fmt: skip


# ─── Decision ────────────────────────────────────────────────────────────


def decide(req: SendRequest, *, now: datetime | None = None) -> Decision:
    reasons: list[Reason] = []
    fresh: _Fresh | None = None
    try:
        fresh = _load(req)
    except Exception as exc:
        log.exception("policy: could not load send %s", req.job_ref)
        reasons.append(Reason(0, "POLICY_ERROR", HOLD, f"could not load state: {exc!r}"))

    if fresh is not None:
        for number, check in CHECKS:
            try:
                reasons.extend(check(req, fresh))
            except Exception as exc:
                log.exception("policy: check %s failed for %s", number, req.job_ref)
                reasons.append(Reason(number, "POLICY_ERROR", HOLD, f"check failed: {exc!r}"))
        if not reasons:
            try:
                reasons.extend(_check_limits(req, fresh, now))
            except Exception as exc:
                log.exception("policy: limits failed for %s", req.job_ref)
                reasons.append(Reason(6, "POLICY_ERROR", HOLD, f"check failed: {exc!r}"))

    reasons.sort(key=lambda r: r.check)
    outcome = reasons[0].outcome if reasons else SEND
    return _record(req, fresh, outcome, reasons)


def _record(
    req: SendRequest, fresh: _Fresh | None, outcome: str, reasons: list[Reason]
) -> Decision:
    try:
        row = PolicyDecision.objects.create(
            outcome=outcome,
            reasons=[r.as_dict() for r in reasons],
            purpose=req.purpose,
            email_hash=email_hash(fresh.recipient) if fresh and fresh.recipient else "",
            job_ref=req.job_ref,
            sending_domain=fresh.domain if fresh else None,
            template_version=fresh.version if fresh else None,
        )
    except Exception:
        log.exception("policy: could not record decision for %s", req.job_ref)
        if outcome == SEND:
            reasons = [Reason(0, "POLICY_LOG_FAILED", HOLD, "decision could not be logged")]
            outcome = HOLD
        return Decision(outcome, tuple(reasons), None)

    log.info("policy decision", extra={"job_ref": req.job_ref, "outcome": outcome,
                                       "codes": [r.code for r in reasons], "decision_id": str(row.pk)})  # fmt: skip
    return Decision(outcome, tuple(reasons), row.pk)
