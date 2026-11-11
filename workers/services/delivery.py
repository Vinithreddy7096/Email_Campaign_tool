"""
Delivery pipeline (BUILD.md §10.7): expand → render → final check → handoff.

Each stage locks the job row, checks the state it expects (so a duplicated or
replayed task is a no-op), and records every state change as a
RecipientJobTransition in the same transaction.

Handoff runs in two transactions with the SMTP call between them. The job is
committed as SUBMITTED_TO_MTA *before* the network call, so a worker that dies
mid-submission leaves a job that looks "maybe sent" — which is exactly how it is
treated (Q-DEL-1: held for review, never resent automatically).

Nothing here chooses an MTA by recipient: every submission goes to the one
configured submission listener (MTA_SMTP_*). In MOCK_MODE that is the sink.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import AuditEvent
from campaigns.models import Campaign, CampaignSnapshotRecipient
from campaigns.preflight import contact_merge_data
from common.models import Stream
from contacts.models import Contact
from delivery.models import Message, RecipientJob, RecipientJobTransition, SendAttempt
from email_templates.rendering import RenderedMessage, RenderError, render
from suppression.unsubscribe import unsubscribe_url
from workers.services import smtp_submit
from workers.services.mime import build_message, make_message_id
from workers.services.policy import SendRequest, decide
from workers.services.verp import verp_address

log = logging.getLogger(__name__)
State = RecipientJob.State
SYSTEM = "celery:delivery"


class InvalidJobTransition(Exception):
    pass


# ─── State changes ───────────────────────────────────────────────────────


def _transition(job: RecipientJob, to: str, *, code: str, detail: str = "",
                actor: Any = None) -> None:  # fmt: skip
    """Caller holds the row lock and saves ``job`` afterwards."""
    allowed = RecipientJob.TRANSITIONS.get(job.state, ())
    if to not in allowed:
        raise InvalidJobTransition(f"{job.state} → {to} is not allowed")
    RecipientJobTransition.objects.create(job=job, from_state=job.state, to_state=to, code=code,
                                          detail=detail[:2000], actor=actor)  # fmt: skip
    job.state = to


def _locked(job_id: Any) -> RecipientJob:
    return (
        RecipientJob.objects.select_for_update(of=("self",))
        .select_related("campaign", "sender_identity__domain", "template_version", "contact")
        .get(pk=job_id)
    )


def campaign_job_key(campaign_id: Any, contact_id: Any, version_id: Any) -> str:
    """§10.7.1: SHA-256 of campaign_id|contact_id|template_version_id."""
    return hashlib.sha256(f"{campaign_id}|{contact_id}|{version_id}".encode()).hexdigest()


# ─── 1. Expand ───────────────────────────────────────────────────────────


def dispatch_due_campaigns(now: datetime | None = None) -> list[uuid.UUID]:
    """SCHEDULED campaigns whose time has come → SENDING. Also returns SENDING
    campaigns whose expansion is incomplete (a crashed expander resumes)."""
    now = now or timezone.now()
    ready: list[uuid.UUID] = []
    for campaign in Campaign.objects.filter(
        status=Campaign.Status.SCHEDULED, scheduled_at__lte=now
    ):
        with transaction.atomic():
            c = Campaign.objects.select_for_update().get(pk=campaign.pk)
            if c.status != Campaign.Status.SCHEDULED:
                continue
            c.status = Campaign.Status.SENDING
            c.save(update_fields=["status", "updated_at"])
            AuditEvent.record("campaign.dispatch", actor_label=SYSTEM, target=c,
                              before={"status": "SCHEDULED"}, after={"status": "SENDING"})  # fmt: skip
        ready.append(c.pk)
    for c in Campaign.objects.filter(status=Campaign.Status.SENDING).exclude(pk__in=ready):
        if c.jobs.count() < c.snapshot.recipient_count:
            ready.append(c.pk)
    return ready


def expand_campaign(campaign_id: Any, *, batch_size: int | None = None) -> list[uuid.UUID]:
    """Snapshot → RecipientJobs (§10.6.4), idempotent on idempotency_key.
    Returns ids of jobs created by this call, now QUEUED for rendering."""
    batch_size = batch_size or settings.CAMPAIGN_EXPANSION_BATCH_SIZE
    campaign = Campaign.objects.select_related("snapshot").get(pk=campaign_id)
    snapshot = campaign.snapshot
    created: list[uuid.UUID] = []
    recipients = (CampaignSnapshotRecipient.objects.filter(snapshot=snapshot)
                  .select_related("contact").order_by("id"))  # fmt: skip
    batch: list[CampaignSnapshotRecipient] = []
    for row in recipients.iterator(chunk_size=batch_size):
        batch.append(row)
        if len(batch) >= batch_size:
            created += _expand_batch(campaign, batch)
            batch = []
            campaign.refresh_from_db(fields=["status"])
            if campaign.status != Campaign.Status.SENDING:
                return created  # paused or canceled mid-expansion; resumes later
    if batch:
        created += _expand_batch(campaign, batch)
    return created


def _expand_batch(campaign: Campaign, rows: list[CampaignSnapshotRecipient]) -> list[uuid.UUID]:
    snapshot = campaign.snapshot
    keys = {
        campaign_job_key(campaign.pk, r.contact_id, snapshot.template_version_id): r for r in rows
    }
    with transaction.atomic():
        existing = set(RecipientJob.objects.filter(idempotency_key__in=keys)
                       .values_list("idempotency_key", flat=True))  # fmt: skip
        jobs = []
        for key, row in keys.items():
            if key in existing:
                continue
            contact: Contact = row.contact
            erased = contact.deleted_at is not None or not contact.email
            jobs.append(RecipientJob(
                idempotency_key=key, purpose=campaign.purpose, campaign=campaign,
                contact=contact, contact_list=snapshot.contact_list,
                recipient="" if erased else (contact.email or ""), email_hash=row.email_hash,
                sender_identity_id=campaign.sender_identity_id,
                template_version_id=snapshot.template_version_id,
                expected_checksum=snapshot.template_checksum, from_snapshot=True,
                state=State.SUPPRESSED if erased else State.QUEUED,
            ))  # fmt: skip
        RecipientJob.objects.bulk_create(jobs, ignore_conflicts=True)
        inserted = {j.pk for j in RecipientJob.objects.filter(pk__in=[j.pk for j in jobs])}
        transitions = []
        for job in jobs:
            if job.pk not in inserted:
                continue  # a concurrent expander won the race for this key
            if job.state == State.SUPPRESSED:
                transitions.append(RecipientJobTransition(job=job, from_state=State.DRAFT,
                                   to_state=State.SUPPRESSED, code="CONTACT_ERASED"))  # fmt: skip
            else:
                transitions += [
                    RecipientJobTransition(job=job, from_state=State.DRAFT,
                                           to_state=State.ELIGIBLE, code="EXPANDED"),
                    RecipientJobTransition(job=job, from_state=State.ELIGIBLE,
                                           to_state=State.QUEUED, code="QUEUED_FOR_RENDER"),
                ]  # fmt: skip
        RecipientJobTransition.objects.bulk_create(transitions)
    return [j.pk for j in jobs if j.pk in inserted and j.state == State.QUEUED]


# ─── 2. Render ───────────────────────────────────────────────────────────


def _render(job: RecipientJob) -> RenderedMessage:
    """Deterministic (§10.2): render() at handoff reproduces render() here.
    Campaign mail merges the contact's data; transactional mail merges what the
    API caller sent (§10.6.6)."""
    if job.purpose == Stream.TRANSACTIONAL:
        merge = dict(job.merge_data)
    else:
        merge = contact_merge_data(job.contact) if job.contact else {}
    system = {}
    if job.contact_id:
        system["unsubscribe_url"] = unsubscribe_url(job.contact_id, job.campaign_id or "")
    return render(job.template_version, merge, system, require_approved=False)


def render_job(job_id: Any) -> RecipientJob:
    with transaction.atomic():
        job = _locked(job_id)
        if job.state != State.QUEUED or job.review_required:
            return job
        try:
            rendered = _render(job)
        except RenderError as exc:
            job.review_required = True
            job.review_reason = f"render failed: {exc}"
            job.save(update_fields=["review_required", "review_reason", "updated_at"])
            return job
        job.content_hash = rendered.content_hash
        job.template_checksum = rendered.template_checksum
        _transition(job, State.RENDERED, code="RENDERED")
        job.save()
    return job


# ─── 3. Final check + handoff ────────────────────────────────────────────


def _send_request(job: RecipientJob) -> SendRequest:
    return SendRequest(
        recipient=job.recipient, purpose=job.purpose, sender=job.sender_identity,
        template_version=job.template_version, expected_checksum=job.expected_checksum,
        from_snapshot=job.from_snapshot, contact=job.contact, contact_list=job.contact_list,
        campaign_ref=str(job.campaign_id) if job.campaign_id else "",
        api_client_ref=job.api_client_ref, job_ref=str(job.pk),
    )  # fmt: skip


def handoff_job(job_id: Any, *, now: datetime | None = None,
                smtp_class: Callable[..., Any] | None = None) -> RecipientJob:  # fmt: skip
    now = now or timezone.now()
    with transaction.atomic():
        job = _locked(job_id)
        if job.state not in (State.RENDERED, State.DEFERRED) or job.review_required:
            return job
        if job.next_attempt_at and job.next_attempt_at > now:
            return job
        if job.campaign and job.campaign.status == Campaign.Status.CANCELED:
            _transition(job, State.CANCELED, code="CAMPAIGN_CANCELED")
            job.save()
            return job
        if now - job.created_at > timedelta(hours=settings.MESSAGE_MAX_AGE_HOURS):
            _transition(job, State.EXPIRED, code="MAX_AGE",
                        detail=f"older than MESSAGE_MAX_AGE_HOURS={settings.MESSAGE_MAX_AGE_HOURS}")  # fmt: skip
            job.save()
            return job

        decision = decide(_send_request(job), now=now)  # §5.1 step 7: immediately before handoff
        if decision.outcome == "SUPPRESS":
            _transition(job, State.SUPPRESSED, code=decision.codes[0],
                        detail="; ".join(r.detail for r in decision.reasons))  # fmt: skip
            job.save()
            return job
        if decision.outcome == "HOLD":
            wait = decision.retry_after_s or settings.DELIVERY_HOLD_RETRY_SECONDS
            job.next_attempt_at = now + timedelta(seconds=wait)
            job.save(update_fields=["next_attempt_at", "updated_at"])
            return job

        try:
            rendered = _render(job)
        except RenderError as exc:
            return _flag(job, f"render failed at handoff: {exc}")
        if rendered.content_hash != job.content_hash:
            # The template is pinned by checksum (policy check 5), so a change
            # here is the recipient's own data, edited since render. Send the
            # current content and record its hash: Message.content_hash must
            # describe what was actually sent.
            log.info("job %s re-rendered with changed merge data", job.pk)
            job.content_hash = rendered.content_hash

        attempt = job.attempts + 1
        domain = job.sender_identity.domain
        message_id = make_message_id(
            job.pk.hex, attempt, settings.MESSAGE_ID_DOMAIN or domain.domain
        )
        envelope_from = verp_address(job.pk, domain.return_path_domain)
        unsub = None
        if job.purpose == Stream.MARKETING and job.contact_id:
            unsub = unsubscribe_url(job.contact_id, job.campaign_id or "")
        payload = build_message(
            rendered=rendered, from_address=job.sender_identity.from_address,
            display_name=job.sender_identity.display_name, reply_to=job.sender_identity.reply_to,
            recipient=job.recipient, message_id=message_id, date=now, unsubscribe_url=unsub,
        ).as_bytes()  # fmt: skip
        job.attempts = attempt
        job.next_attempt_at = None
        _transition(job, State.SUBMITTED_TO_MTA, code="SUBMITTING", detail=f"attempt {attempt}")
        job.save()

    # The network call happens with nothing locked and the state committed.
    result = smtp_submit.submit(
        envelope_from=envelope_from, recipient=job.recipient, payload=payload,
        host=settings.MTA_SMTP_HOST, port=settings.MTA_SMTP_PORT,
        timeout=settings.MTA_SUBMISSION_TIMEOUT_S, use_tls=settings.MTA_SMTP_USE_TLS,
        username=settings.MTA_SMTP_USERNAME, password=settings.MTA_SMTP_PASSWORD,
        **({"smtp_class": smtp_class} if smtp_class else {}),
    )  # fmt: skip
    return _record_result(job.pk, attempt, result, message_id=message_id,
                          envelope_from=envelope_from, payload=payload, now=now)  # fmt: skip


def _flag(job: RecipientJob, reason: str) -> RecipientJob:
    job.review_required = True
    job.review_reason = reason
    job.save(update_fields=["review_required", "review_reason", "updated_at"])
    return job


def _record_result(job_id: Any, attempt: int, result: smtp_submit.SubmitResult, *,
                   message_id: str, envelope_from: str, payload: bytes,
                   now: datetime) -> RecipientJob:  # fmt: skip
    with transaction.atomic():
        job = _locked(job_id)
        SendAttempt.objects.create(job=job, number=attempt, outcome=result.outcome,
                                   smtp_code=result.code, smtp_response=result.response[:2000],
                                   mta_host=f"{settings.MTA_SMTP_HOST}:{settings.MTA_SMTP_PORT}")  # fmt: skip
        code = f"SMTP_{result.code}" if result.code else f"SMTP_{result.outcome}"
        if result.outcome == smtp_submit.ACCEPTED:
            Message.objects.create(job=job, message_id=message_id, mta_queue_id=result.queue_id,
                                   envelope_from=envelope_from, content_hash=job.content_hash,
                                   size_bytes=len(payload), accepted_at=timezone.now())  # fmt: skip
            _transition(job, State.ACCEPTED, code=code, detail=result.response)
        elif result.outcome == smtp_submit.DEFERRED:
            _transition(job, State.DEFERRED, code=code, detail=result.response)
            if attempt >= settings.MAX_DELIVERY_ATTEMPTS:
                _transition(job, State.EXPIRED, code="MAX_ATTEMPTS",
                            detail=f"{attempt} attempts (MAX_DELIVERY_ATTEMPTS)")  # fmt: skip
            else:
                backoff = settings.RETRY_BACKOFF_SECONDS
                job.next_attempt_at = now + timedelta(
                    seconds=backoff[min(attempt, len(backoff)) - 1]
                )
        elif result.outcome == smtp_submit.REJECTED:
            # Suppression from permanent failures is the event pipeline's job
            # (§10.8.2, Q-EVT-3); a submission-time 5xx is recorded here.
            _transition(job, State.BOUNCED, code=code, detail=result.response)
        else:  # UNKNOWN: maybe sent. Q-DEL-1 default: hold for review, never resend.
            job.review_required = True
            job.review_reason = result.response
        job.save()
    return job


# ─── Sweeps ──────────────────────────────────────────────────────────────


def due_job_ids(now: datetime | None = None, limit: int = 1000) -> list[uuid.UUID]:
    now = now or timezone.now()
    from django.db.models import Q

    return list(
        RecipientJob.objects.filter(
            state__in=[State.RENDERED, State.DEFERRED], review_required=False
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:limit]
    )


def stale_queued_ids(now: datetime | None = None, limit: int = 1000) -> list[uuid.UUID]:
    """QUEUED jobs whose render task never ran (lost message, worker restart)."""
    now = now or timezone.now()
    return list(
        RecipientJob.objects.filter(state=State.QUEUED, review_required=False,
                                    updated_at__lt=now - timedelta(minutes=5))
        .values_list("pk", flat=True)[:limit]
    )  # fmt: skip


def flag_stuck_submissions(now: datetime | None = None) -> int:
    """A job left in SUBMITTED_TO_MTA well past the SMTP timeout means the worker
    died mid-submission: maybe sent. Flag it rather than guess."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=2 * settings.MTA_SUBMISSION_TIMEOUT_S + 60)
    return RecipientJob.objects.filter(
        state=State.SUBMITTED_TO_MTA, review_required=False, updated_at__lt=cutoff
    ).update(review_required=True,
             review_reason="no submission result recorded; the worker may have died mid-send")  # fmt: skip


def finalize_campaigns() -> list[uuid.UUID]:
    """SENDING → COMPLETED once every snapshot recipient has a terminal job.
    Canceled campaigns: queued jobs become CANCELED."""
    done: list[uuid.UUID] = []
    for c in Campaign.objects.filter(status=Campaign.Status.CANCELED,
                                     jobs__state__in=[State.QUEUED, State.RENDERED, State.DEFERRED]).distinct():  # fmt: skip
        for job_id in c.jobs.filter(
            state__in=[State.QUEUED, State.RENDERED, State.DEFERRED]
        ).values_list("pk", flat=True):
            with transaction.atomic():
                job = _locked(job_id)
                if job.state in (State.QUEUED, State.RENDERED, State.DEFERRED):
                    _transition(job, State.CANCELED, code="CAMPAIGN_CANCELED")
                    job.save()
    for c in Campaign.objects.filter(status=Campaign.Status.SENDING).select_related("snapshot"):
        total = c.jobs.count()
        open_jobs = c.jobs.exclude(state__in=RecipientJob.TERMINAL).count()
        if total >= c.snapshot.recipient_count and open_jobs == 0:
            with transaction.atomic():
                locked = Campaign.objects.select_for_update().get(pk=c.pk)
                if locked.status != Campaign.Status.SENDING:
                    continue
                locked.status = Campaign.Status.COMPLETED
                locked.completed_at = timezone.now()
                locked.save(update_fields=["status", "completed_at", "updated_at"])
                AuditEvent.record("campaign.complete", actor_label=SYSTEM, target=locked,
                                  before={"status": "SENDING"}, after={"status": "COMPLETED"})  # fmt: skip
            done.append(c.pk)
    return done


# ─── Operator review (Q-DEL-1) ───────────────────────────────────────────

RESOLUTIONS = ("accepted", "cancel", "resend")


def resolve_review(job_id: Any, resolution: str, *, actor: Any, note: str = "") -> RecipientJob:
    """accepted: the MTA log shows it was taken; cancel: do not send;
    resend: the operator is sure it was not delivered (risk of a duplicate)."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")
    with transaction.atomic():
        job = _locked(job_id)
        if not job.review_required:
            raise InvalidJobTransition(f"{job} is not awaiting review")
        before = job.state
        detail = f"operator review: {note}".strip()
        if resolution == "accepted":
            _transition(job, State.ACCEPTED, code="REVIEW_ACCEPTED", detail=detail, actor=actor)
        elif resolution == "cancel":
            _transition(job, State.CANCELED, code="REVIEW_CANCELED", detail=detail, actor=actor)
        elif job.state == State.SUBMITTED_TO_MTA:
            _transition(job, State.RENDERED, code="REVIEW_RESEND", detail=detail, actor=actor)
        job.review_required = False
        job.next_attempt_at = None
        job.save()
        AuditEvent.record("delivery.review", actor=actor, target=job, before={"state": before},
                          after={"state": job.state}, resolution=resolution, note=note)  # fmt: skip
    return job


def state_counts(campaign_id: Any) -> dict[str, int]:
    from django.db.models import Count

    rows = (
        RecipientJob.objects.filter(campaign_id=campaign_id).values("state").annotate(n=Count("pk"))
    )
    return {row["state"]: row["n"] for row in rows}
