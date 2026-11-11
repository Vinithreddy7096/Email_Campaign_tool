"""Campaign lifecycle (BUILD.md §10.6.2). The admin and the API both call these,
so permission checks, preflight, snapshots and audit happen in one place.

Every transition locks the campaign row, checks the actor's permission, and
writes an audit event. Nothing here sends: dispatch is the delivery pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import AuditEvent
from campaigns import preflight
from campaigns.models import Campaign, CampaignSnapshot, CampaignSnapshotRecipient
from common.models import Stream
from consent.evaluation import marketing_consent_bulk
from contacts.models import ContactList, Segment
from delivery.models import RateLimit, SendPause
from domains.models import SenderIdentity, SendingDomain
from email_templates.models import TemplateVersion

Status = Campaign.Status
_SNAPSHOT_BATCH = 2000
ACCEPTED_STILL_COMPLETE = (
    "Messages the mail server has already accepted will still be delivered; "
    "pausing only stops what has not been handed off yet."
)


class InvalidTransition(Exception):
    pass


class PreflightFailed(Exception):
    def __init__(self, findings: list[preflight.Finding]):
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings if f.level == preflight.FAIL))


class ScheduleRejected(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class ScheduleResult:
    campaign: Campaign
    warnings: list[str] = field(default_factory=list)


def _require(actor: Any, perm: str) -> None:
    if actor is None or not actor.has_perm(f"campaigns.{perm}"):
        raise PermissionDenied(f"Missing permission campaigns.{perm}")


def _locked(campaign: Campaign) -> Campaign:
    return Campaign.objects.select_for_update().get(pk=campaign.pk)


def _expect(campaign: Campaign, *allowed: str) -> None:
    if campaign.status not in allowed:
        raise InvalidTransition(
            f"{campaign} is {campaign.status}; expected {' or '.join(allowed)}."
        )


def _audit(action: str, actor: Any, campaign: Campaign, before: str, **extra: Any) -> None:
    AuditEvent.record(
        f"campaign.{action}",
        actor=actor,
        target=campaign,
        before={"status": before},
        after={"status": campaign.status, **extra},
    )


def _validate_choice(purpose: str, version: TemplateVersion) -> None:
    """Cheap checks at authoring time; preflight does the thorough ones."""
    if purpose not in Stream.values:
        raise ValidationError({"purpose": f"Unknown purpose {purpose!r}."})
    if version.status != TemplateVersion.Status.APPROVED:
        # §10.6.1: template_version "must be APPROVED".
        raise ValidationError({"template_version": f"{version} is {version.status}, not APPROVED."})


def _validate_audience(audience_list: ContactList, segment: Segment | None) -> None:
    if segment is not None and segment.contact_list_id != audience_list.pk:
        raise ValidationError(
            {"audience_segment": f"Segment {segment.name!r} is not a segment of {audience_list}."}
        )


# ─── Authoring ───────────────────────────────────────────────────────────


@transaction.atomic
def create(
    actor: Any,
    *,
    name: str,
    purpose: str,
    sender_identity: SenderIdentity,
    template_version: TemplateVersion,
    audience_list: ContactList,
    audience_segment: Segment | None = None,
    scheduled_at: datetime | None = None,
) -> Campaign:
    """Creating a campaign never sends (spec §9)."""
    _require(actor, "add_campaign")
    _validate_choice(purpose, template_version)
    _validate_audience(audience_list, audience_segment)
    campaign = Campaign.objects.create(
        name=name,
        owner=actor,
        purpose=purpose,
        sender_identity=sender_identity,
        template_version=template_version,
        audience_list=audience_list,
        audience_segment=audience_segment,
        scheduled_at=scheduled_at,
    )
    AuditEvent.record("campaign.create", actor=actor, target=campaign,
                      after={"status": campaign.status, "name": name, "purpose": purpose})  # fmt: skip
    return campaign


EDITABLE_FIELDS = ("name", "purpose", "sender_identity", "template_version", "audience_list",
                   "audience_segment", "scheduled_at")  # fmt: skip


@transaction.atomic
def update(actor: Any, campaign: Campaign, **changes: Any) -> Campaign:
    _require(actor, "change_campaign")
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"Not editable: {', '.join(sorted(unknown))}")
    current = _locked(campaign)
    if not current.is_editable:
        raise InvalidTransition(f"{current} is {current.status}; only drafts can be edited.")
    for name, value in changes.items():
        setattr(current, name, value)
    _validate_choice(current.purpose, current.template_version)
    _validate_audience(current.audience_list, current.audience_segment)
    current.status = Status.DRAFT  # an edit invalidates any earlier preflight
    current.preflight_report = {}
    current.save()
    AuditEvent.record("campaign.edit", actor=actor, target=current,
                      after={k: str(v) for k, v in changes.items()})  # fmt: skip
    return current


def submit(actor: Any, campaign: Campaign) -> Campaign:
    """Run preflight; READY_FOR_REVIEW on success, PREFLIGHT_FAILED otherwise.
    The report is saved either way so the author can see it."""
    _require(actor, "submit_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, *Campaign.EDITABLE)
        before = current.status
        result = preflight.run(current)
        current.preflight_report = result.report()
        current.status = Status.PREFLIGHT_FAILED if result.failed else Status.READY_FOR_REVIEW
        current.submitted_by = actor
        current.submitted_at = timezone.now()
        current.save()
        _audit("submit", actor, current, before)
    if result.failed:
        raise PreflightFailed(result.findings)
    return current


# ─── Review ──────────────────────────────────────────────────────────────


def approve(actor: Any, campaign: Campaign, note: str = "") -> Campaign:
    """Re-run preflight and freeze the snapshot (spec §9 approve)."""
    _require(actor, "approve_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.READY_FOR_REVIEW)
        before = current.status
        result = preflight.run(current)
        current.preflight_report = result.report()
        if result.failed:
            # The world changed since submission; send it back to the author.
            current.status = Status.PREFLIGHT_FAILED
            current.save()
            _audit("approve_failed_preflight", actor, current, before)
        else:
            _freeze_snapshot(current, actor, result)
            current.status = Status.APPROVED
            current.reviewed_by = actor
            current.reviewed_at = timezone.now()
            current.review_note = note
            current.save()
            _audit("approve", actor, current, before, note=note,
                   recipients=len(result.audience.recipients))  # fmt: skip
    if result.failed:
        raise PreflightFailed(result.findings)
    return current


def _freeze_snapshot(campaign: Campaign, actor: Any, result: preflight.Preflight) -> None:
    sender = campaign.sender_identity
    version = campaign.template_version
    snapshot = CampaignSnapshot.objects.create(
        campaign=campaign,
        template_version=version,
        template_checksum=version.checksum,
        sender={
            "sender_identity_id": str(sender.pk),
            "from_address": sender.from_address,
            "display_name": sender.display_name,
            "reply_to": sender.reply_to,
            "domain": sender.domain.domain,
        },
        sending_domain=sender.domain,
        contact_list=campaign.audience_list,
        segment=campaign.audience_segment,
        segment_definition=(
            campaign.audience_segment.definition if campaign.audience_segment else None
        ),
        preflight_report=result.report(),
        approved_by=actor,
        audience_total=result.audience.total,
        excluded_suppressed=result.audience.excluded_suppressed,
        recipient_count=len(result.audience.recipients),
    )
    rows = [
        CampaignSnapshotRecipient(
            snapshot=snapshot, contact_id=cid, email_hash=h, consent_record_id=consent_id
        )  # fmt: skip
        for cid, h, consent_id in result.audience.recipients
    ]
    CampaignSnapshotRecipient.objects.bulk_create(rows, batch_size=_SNAPSHOT_BATCH)


def _review(actor: Any, campaign: Campaign, *, to: str, note: str, action: str) -> Campaign:
    _require(actor, "approve_campaign")
    if not note.strip():
        raise ValidationError("A note is required when rejecting or requesting changes.")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.READY_FOR_REVIEW)
        before = current.status
        current.status = to
        current.reviewed_by = actor
        current.reviewed_at = timezone.now()
        current.review_note = note.strip()
        current.save()
        _audit(action, actor, current, before, note=current.review_note)
    return current


def reject(actor: Any, campaign: Campaign, note: str) -> Campaign:
    return _review(actor, campaign, to=Status.REJECTED, note=note, action="reject")


def request_changes(actor: Any, campaign: Campaign, note: str) -> Campaign:
    return _review(
        actor, campaign, to=Status.CHANGES_REQUESTED, note=note, action="request_changes"
    )


# ─── Scheduling and control ──────────────────────────────────────────────


def schedule(actor: Any, campaign: Campaign, at: datetime | None = None) -> ScheduleResult:
    """Reject when domain, authentication, consent or warm-up controls fail (§9)."""
    _require(actor, "schedule_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.APPROVED)
        when = at or current.scheduled_at or timezone.now()
        reasons, warnings = _schedule_checks(current, when)
        if reasons:
            raise ScheduleRejected(reasons)
        before = current.status
        current.status = Status.SCHEDULED
        current.scheduled_at = when
        current.save()
        _audit("schedule", actor, current, before, scheduled_at=when.isoformat(), warnings=warnings)
    return ScheduleResult(current, warnings)


def _schedule_checks(campaign: Campaign, when: datetime) -> tuple[list[str], list[str]]:
    snapshot = campaign.snapshot
    domain = SendingDomain.objects.get(pk=snapshot.sending_domain_id)
    version = TemplateVersion.objects.get(pk=snapshot.template_version_id)
    reasons: list[str] = []
    warnings: list[str] = []

    if domain.status != SendingDomain.Status.ACTIVE:
        reasons.append(f"Sending domain {domain} is {domain.status}, not ACTIVE.")
    if not domain.dns_verified:
        reasons.append(f"SPF, DKIM and DMARC are not all verified for {domain}.")
    if version.status != TemplateVersion.Status.APPROVED:
        reasons.append(f"{version} is {version.status}; schedule needs an approved template.")
    if when < timezone.now() - timedelta(minutes=1):
        reasons.append("Scheduled time is in the past.")

    # Consent: who still has it now. Lost consent is suppressed at send time;
    # scheduling is refused only if nobody is left to send to.
    if campaign.purpose == Stream.MARKETING:
        ids = list(snapshot.recipients.values_list("contact_id", flat=True))
        verdicts = marketing_consent_bulk(ids, snapshot.contact_list)
        lost = sum(1 for v in verdicts.values() if not v.granted)
        if ids and lost == len(ids):
            reasons.append("No recipient in the snapshot still has marketing consent.")
        elif lost:
            warnings.append(
                f"{lost} recipient(s) lost consent since approval and will be suppressed."
            )

    reasons_w, warnings_w = _limit_checks(campaign, domain, when)
    reasons += reasons_w
    warnings += warnings_w
    if not settings.GLOBAL_SEND_ENABLED:
        warnings.append("GLOBAL_SEND_ENABLED is false: nothing will be handed to the MTA.")
    return reasons, warnings


def _limit_checks(campaign: Campaign, domain: SendingDomain, when: datetime) -> tuple[list[str], list[str]]:  # fmt: skip
    """Warm-up (§9 "schedule rejects when ... warm-up controls fail"): refuse an
    audience the warm-up cannot send before its jobs expire. Rate limits only
    slow a send down, so they are a warning."""
    from workers.services import ratelimit

    reasons: list[str] = []
    warnings: list[str] = []
    audience = campaign.snapshot.recipients.count()
    max_age = settings.MESSAGE_MAX_AGE_HOURS
    capacity, plans = ratelimit.warmup_capacity(
        str(domain.pk), str(domain.ip_pool_id or ""), when, when + timedelta(hours=max_age)
    )
    if capacity is not None and audience > capacity:
        reasons.append(
            f"Warm-up allows {capacity} message(s) from {domain} in the {max_age} h before "
            f"queued jobs expire (MESSAGE_MAX_AGE_HOURS); the audience is {audience}. "
            "Split the audience or schedule it later."
        )
    if any(p.recipient_domains for p in plans):
        warnings.append("A provider-specific warm-up plan applies; mail to that provider may "
                        "wait for later days and can expire.")  # fmt: skip
    if not RateLimit.objects.filter(is_active=True).exists():
        warnings.append("No rate limits are configured (Q-POL-1).")
    if not plans:
        warnings.append(f"No warm-up plan is configured for {domain} or its IP pool (Q-POL-1).")
    return reasons, warnings


def pause(actor: Any, campaign: Campaign, reason: str) -> Campaign:
    """Stop future dispatch. Also raises a campaign SendPause, so jobs already
    queued are held by the policy service at handoff."""
    _require(actor, "pause_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.SCHEDULED, Status.SENDING)
        before = current.status
        current.active_pause = SendPause.pause(
            SendPause.Scope.CAMPAIGN, str(current.pk), reason=reason or "campaign paused", by=actor
        )
        current.paused_from = before
        current.status = Status.PAUSED
        current.save()
        _audit("pause", actor, current, before, note=ACCEPTED_STILL_COMPLETE)
    return current


def resume(actor: Any, campaign: Campaign) -> Campaign:
    _require(actor, "pause_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.PAUSED)
        if current.active_pause is not None:
            current.active_pause.resume(by=actor)  # only the pause this campaign raised
        current.status = current.paused_from
        current.paused_from = ""
        current.active_pause = None
        current.save()
        _audit("resume", actor, current, Status.PAUSED)
    return current


def cancel(actor: Any, campaign: Campaign, reason: str) -> Campaign:
    """Permanent. A campaign SendPause that is never lifted holds anything
    still queued; the delivery pipeline marks those jobs CANCELED."""
    _require(actor, "pause_campaign")
    with transaction.atomic():
        current = _locked(campaign)
        _expect(current, Status.APPROVED, Status.SCHEDULED, Status.SENDING, Status.PAUSED)
        before = current.status
        if current.active_pause is None:
            current.active_pause = SendPause.pause(
                SendPause.Scope.CAMPAIGN, str(current.pk),
                reason=f"campaign canceled: {reason or 'no reason given'}", by=actor,
            )  # fmt: skip
        current.status = Status.CANCELED
        current.paused_from = ""
        current.canceled_at = timezone.now()
        current.save()
        _audit("cancel", actor, current, before, reason=reason, note=ACCEPTED_STILL_COMPLETE)
    return current
