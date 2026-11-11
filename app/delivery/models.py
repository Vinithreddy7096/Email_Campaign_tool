"""Delivery data: recipient jobs and their history (BUILD.md §10.7), pause
flags, rate limits, warm-up plans and the policy decision log (§10.9).

A RecipientJob is one message to one recipient. Its state follows Appendix B;
every change is appended to RecipientJobTransition in the same transaction.
Retries add SendAttempt rows — never a second job (§5.2 idempotency).
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from audit.models import AuditEvent
from common.models import AppendOnlyModel, NoDeleteModel, NoDeleteQuerySet, Stream


class SendPauseQuerySet(NoDeleteQuerySet):
    def active(self) -> SendPauseQuerySet:
        return self.filter(lifted_at__isnull=True)


SendPauseManager = models.Manager.from_queryset(SendPauseQuerySet)


class SendPause(NoDeleteModel):
    """An emergency stop (§10.9 check 1). While active, matching sends HOLD.

    Sending domains and IP pools also have their own status flags (PAUSED,
    is_active), which the policy checks too; a SendPause is how an operator or
    an automatic shutdown (§12, thresholds Q-EVT-2) stops something without
    editing configuration.
    """

    class Scope(models.TextChoices):
        GLOBAL = "GLOBAL", "Everything"
        STREAM = "STREAM", "One stream (route)"
        DOMAIN = "DOMAIN", "One sending domain"
        IP_POOL = "IP_POOL", "One IP pool"
        CAMPAIGN = "CAMPAIGN", "One campaign"
        API_CLIENT = "API_CLIENT", "One API client (token)"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    # Stream name, SendingDomain / IpPool / campaign id, or ApiToken id.
    # Empty only for GLOBAL. A string so it can name things built later.
    target = models.CharField(max_length=64, blank=True)
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Who or what raised it when not a user, e.g. "auto:complaint-rate".
    created_by_label = models.CharField(max_length=128, blank=True)

    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    objects: ClassVar[SendPauseManager] = SendPauseManager()

    class Meta:
        db_table = "delivery_send_pause"
        indexes = [
            models.Index(
                fields=["scope", "target"],
                condition=models.Q(lifted_at=None),
                name="delivery_pause_active_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(scope="GLOBAL") & models.Q(target=""))
                | (~models.Q(scope="GLOBAL") & ~models.Q(target="")),
                name="delivery_send_pause_target_shape",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""), name="delivery_send_pause_has_reason"
            ),
        ]

    def __str__(self) -> str:
        what = "everything" if self.scope == self.Scope.GLOBAL else f"{self.scope} {self.target}"
        state = "resumed" if self.lifted_at else "ACTIVE"
        return f"Pause {what} ({state})"

    @classmethod
    def pause(
        cls, scope: str, target: str = "", *, reason: str, by: Any = None, by_label: str = ""
    ) -> SendPause:
        with transaction.atomic():
            row = cls.objects.create(
                scope=scope,
                target=str(target),
                reason=reason.strip(),
                created_by=by,
                created_by_label=by_label,
            )
            AuditEvent.record(
                "send.pause",
                actor=by,
                actor_label="" if by else (by_label or "system"),
                target=row,
                scope=scope,
                paused=str(target),
                reason=row.reason,
            )
        return row

    def resume(self, *, by: Any) -> None:
        if self.lifted_at is not None:
            return
        with transaction.atomic():
            self.lifted_at = timezone.now()
            self.lifted_by = by
            self.save(update_fields=["lifted_at", "lifted_by", "updated_at"])
            AuditEvent.record("send.resume", actor=by, target=self, scope=self.scope)


def _domain_list(values: Any) -> list[str]:
    from common.email import normalize_domain

    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        raise ValidationError("Recipient domains must be a list of domain names.")
    return sorted({normalize_domain(v) for v in values})


class _Retirable(NoDeleteModel):
    """A control that is switched off, never deleted, so the audit log can
    always say which limits were in force when a message was held."""

    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    audit_name = ""

    class Meta:
        abstract = True

    @classmethod
    def add(cls, *, by: Any, **fields: Any) -> Any:
        row = cls(created_by=by, **fields)
        row.full_clean()
        with transaction.atomic():
            row.save()
            AuditEvent.record(f"{cls.audit_name}.add", actor=by, target=row, after=row.describe())
        return row

    def disable(self, *, by: Any) -> None:
        if not self.is_active:
            return
        with transaction.atomic():
            self.is_active = False
            self.disabled_at = timezone.now()
            self.disabled_by = by
            self.save(update_fields=["is_active", "disabled_at", "disabled_by", "updated_at"])
            AuditEvent.record(f"{self.audit_name}.disable", actor=by, target=self,
                              before=self.describe())  # fmt: skip

    def describe(self) -> str:
        raise NotImplementedError


class RateLimit(_Retirable):
    """§10.9 check 6: at most ``max_messages`` hand-offs per ``per_seconds``.

    No numbers are built in (Q-POL-1): with no active rows nothing is limited
    and the console says so. A row with an empty target applies to each
    campaign / domain / pool / client / recipient domain separately; a row
    with a target applies to that one only.
    """

    class Scope(models.TextChoices):
        TENANT = "TENANT", "Whole instance"
        CAMPAIGN = "CAMPAIGN", "Campaign"
        DOMAIN = "DOMAIN", "Sending domain"
        PROVIDER = "PROVIDER", "Mailbox provider"
        IP_POOL = "IP_POOL", "IP pool"
        API_CLIENT = "API_CLIENT", "API client (credential)"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    # Campaign / SendingDomain / IpPool / ApiToken id; for PROVIDER a label
    # such as "gmail". Empty: each one separately (always empty for TENANT).
    target = models.CharField(max_length=64, blank=True)
    # PROVIDER with a target: the recipient domains that make up the provider.
    recipient_domains = models.JSONField(default=list, blank=True)
    max_messages = models.PositiveIntegerField()
    per_seconds = models.PositiveIntegerField()

    audit_name = "ratelimit"

    class Meta:
        db_table = "delivery_rate_limit"
        constraints = [
            models.CheckConstraint(condition=models.Q(max_messages__gt=0) & models.Q(per_seconds__gt=0),
                                   name="delivery_rate_limit_positive"),
        ]  # fmt: skip

    def __str__(self) -> str:
        return self.describe()

    def clean(self) -> None:
        self.target = self.target.strip()
        if self.scope == self.Scope.TENANT and self.target:
            raise ValidationError("An instance-wide limit has no target.")
        if self.scope == self.Scope.PROVIDER:
            self.recipient_domains = _domain_list(self.recipient_domains or [])
            if bool(self.target) != bool(self.recipient_domains):
                raise ValidationError("A named provider needs its recipient domains; leave both "
                                      "empty to limit each recipient domain separately.")  # fmt: skip
        elif self.recipient_domains:
            raise ValidationError("Recipient domains apply to provider limits only.")
        if not self.max_messages or not self.per_seconds:
            raise ValidationError("A limit needs a positive number of messages and a window.")

    def describe(self) -> str:
        who = self.get_scope_display().lower()
        if self.scope != self.Scope.TENANT:
            who = f"{who} {self.target}" if self.target else f"each {who}"
        return f"{self.max_messages} per {_window(self.per_seconds)} for {who}"


def _window(seconds: int) -> str:
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            n = seconds // size
            return unit if n == 1 else f"{n} {unit}s"
    return f"{seconds} s"


class WarmupPlan(_Retirable):
    """§10.9 check 7: a new domain or IP pool sends at most ``daily_limits[n]``
    messages on day n+1 after ``started_on``; after the last day the plan is
    finished and limits nothing. The volumes are Q-POL-1 — none are built in.

    With ``recipient_domains`` the plan covers mail to that provider only
    (e.g. a slower ramp for one mailbox provider).
    """

    class Scope(models.TextChoices):
        DOMAIN = "DOMAIN", "Sending domain"
        IP_POOL = "IP_POOL", "IP pool"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    target = models.CharField(max_length=64)  # SendingDomain or IpPool id
    started_on = models.DateField()
    daily_limits = models.JSONField()  # [50, 100, 200, ...]: day 1, day 2, ...
    recipient_domains = models.JSONField(default=list, blank=True)

    audit_name = "warmup"

    class Meta:
        db_table = "delivery_warmup_plan"

    def __str__(self) -> str:
        return self.describe()

    def clean(self) -> None:
        if not self.target:
            raise ValidationError("A warm-up plan needs a domain or IP pool.")
        limits = self.daily_limits
        if (not isinstance(limits, list) or not limits
                or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in limits)):  # fmt: skip
            raise ValidationError("Daily limits must be a list of positive whole numbers.")
        self.recipient_domains = _domain_list(self.recipient_domains or [])

    def day_number(self, today: date) -> int:
        return (today - self.started_on).days + 1

    def limit_on(self, day: date) -> int | None:
        """The day's maximum; None once the plan is finished (or not started)."""
        n = self.day_number(day)
        if n < 1:
            return 0  # not started: nothing goes out on this route yet
        return self.daily_limits[n - 1] if n <= len(self.daily_limits) else None

    def describe(self) -> str:
        where = f" to {', '.join(self.recipient_domains)}" if self.recipient_domains else ""
        return (f"warm-up {self.get_scope_display().lower()} {self.target}{where} from "
                f"{self.started_on}: {', '.join(map(str, self.daily_limits))}")  # fmt: skip


class PolicyDecision(AppendOnlyModel):
    """One row per policy.decide() call (§10.9: "every decision is logged with
    its reasons"). Append-only; DB triggers block UPDATE and DELETE."""

    class Outcome(models.TextChoices):
        SEND = "SEND", "Send"
        SUPPRESS = "SUPPRESS", "Suppress"
        HOLD = "HOLD", "Hold"

    outcome = models.CharField(max_length=8, choices=Outcome.choices)
    # [{"check": int, "code": str, "outcome": str, "detail": str}, ...]
    reasons = models.JSONField(default=list)
    purpose = models.CharField(max_length=16, choices=Stream.choices)
    email_hash = models.CharField(max_length=64, blank=True)
    # Caller's reference for the send; becomes a RecipientJob FK in §10.7.
    job_ref = models.CharField(max_length=128, blank=True)
    sending_domain = models.ForeignKey(
        "domains.SendingDomain", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    template_version = models.ForeignKey(
        "email_templates.TemplateVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "delivery_policy_decision"
        indexes = [
            models.Index(fields=["job_ref"]),
            models.Index(fields=["outcome", "-created_at"]),
            models.Index(fields=["email_hash"]),
        ]

    def __str__(self) -> str:
        return f"{self.outcome} {self.job_ref or self.email_hash[:12]}"


class RecipientJob(NoDeleteModel):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ELIGIBLE = "ELIGIBLE", "Eligible"
        QUEUED = "QUEUED", "Queued for rendering"
        RENDERED = "RENDERED", "Rendered"
        SUBMITTED_TO_MTA = "SUBMITTED_TO_MTA", "Submitted to MTA"
        ACCEPTED = "ACCEPTED", "Accepted by MTA"
        DEFERRED = "DEFERRED", "Deferred"
        BOUNCED = "BOUNCED", "Bounced"
        EXPIRED = "EXPIRED", "Expired"
        CANCELED = "CANCELED", "Canceled"
        SUPPRESSED = "SUPPRESSED", "Suppressed"

    TERMINAL = (State.ACCEPTED, State.BOUNCED, State.EXPIRED, State.CANCELED, State.SUPPRESSED)

    # Appendix B, plus the review exits for a "maybe sent" job (Q-DEL-1), and
    # ACCEPTED → BOUNCED for a bounce reported after the MTA took the message
    # (§10.8.2). A post-acceptance deferral is an event only: Postfix retries,
    # and moving the job back would make the app resend.
    TRANSITIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        State.DRAFT: (State.ELIGIBLE, State.CANCELED, State.SUPPRESSED),
        State.ELIGIBLE: (State.QUEUED, State.CANCELED, State.SUPPRESSED),
        State.QUEUED: (State.RENDERED, State.CANCELED, State.SUPPRESSED),
        State.RENDERED: (State.SUBMITTED_TO_MTA, State.SUPPRESSED, State.CANCELED,
                         State.EXPIRED),
        State.SUBMITTED_TO_MTA: (State.ACCEPTED, State.DEFERRED, State.BOUNCED, State.CANCELED,
                                 State.RENDERED),
        State.ACCEPTED: (State.BOUNCED,),
        State.DEFERRED: (State.SUBMITTED_TO_MTA, State.SUPPRESSED, State.CANCELED,
                         State.EXPIRED),
    }  # fmt: skip

    idempotency_key = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=16, choices=Stream.choices)
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, null=True, blank=True, related_name="jobs"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    contact_list = models.ForeignKey(
        "contacts.ContactList", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # The address at expansion time; a later edit to the contact does not
    # redirect a queued message.
    recipient = models.EmailField(max_length=254)
    email_hash = models.CharField(max_length=64)
    sender_identity = models.ForeignKey(
        "domains.SenderIdentity", on_delete=models.PROTECT, related_name="+"
    )
    template_version = models.ForeignKey(
        "email_templates.TemplateVersion", on_delete=models.PROTECT, related_name="+"
    )
    # From the campaign snapshot (or the version itself, for transactional).
    expected_checksum = models.CharField(max_length=64)
    from_snapshot = models.BooleanField(default=False)
    api_client_ref = models.CharField(max_length=64, blank=True)
    # Transactional only (§10.6.6): the caller's merge data, and a hash of the
    # request so a reused idempotency key with a different body is refused.
    # Merge data may be personal; jobs are never deleted but can be redacted.
    merge_data = models.JSONField(default=dict, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True)

    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    # Stored at render (§10.2 "rendered content hash"); handoff re-renders and
    # updates it if the recipient's data changed in between.
    content_hash = models.CharField(max_length=64, blank=True)
    template_checksum = models.CharField(max_length=64, blank=True)
    # A job an operator must look at: a lost SMTP reply after DATA (Q-DEL-1),
    # or a render that failed for this recipient's data.
    review_required = models.BooleanField(default=False)
    review_reason = models.TextField(blank=True)

    class Meta:
        db_table = "delivery_recipient_job"
        indexes = [
            models.Index(fields=["state", "next_attempt_at"]),
            models.Index(fields=["campaign", "state"]),
            models.Index(fields=["review_required"], condition=models.Q(review_required=True),
                         name="delivery_job_review_idx"),
        ]  # fmt: skip

    def __str__(self) -> str:
        return f"{self.state} {self.recipient} ({self.idempotency_key[:12]})"

    @property
    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL


class RecipientJobTransition(AppendOnlyModel):
    job = models.ForeignKey(RecipientJob, on_delete=models.PROTECT, related_name="transitions")
    from_state = models.CharField(max_length=20)
    to_state = models.CharField(max_length=20)
    code = models.CharField(max_length=40)
    detail = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "delivery_job_transition"
        indexes = [models.Index(fields=["job", "created_at"])]

    def __str__(self) -> str:
        return f"{self.from_state} → {self.to_state} ({self.code})"


class SendAttempt(AppendOnlyModel):
    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Accepted (2xx)"
        DEFERRED = "DEFERRED", "Temporary failure (4xx or connection)"
        REJECTED = "REJECTED", "Permanent failure (5xx)"
        UNKNOWN = "UNKNOWN", "Reply lost after DATA — maybe sent"

    job = models.ForeignKey(RecipientJob, on_delete=models.PROTECT, related_name="send_attempts")
    number = models.PositiveIntegerField()
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    smtp_code = models.PositiveIntegerField(null=True, blank=True)
    smtp_response = models.TextField(blank=True)
    mta_host = models.CharField(max_length=255)

    class Meta:
        db_table = "delivery_send_attempt"
        constraints = [
            models.UniqueConstraint(fields=["job", "number"], name="delivery_attempt_number_uniq"),
        ]


class Message(AppendOnlyModel):
    """What the MTA accepted (spec §8 Message)."""

    job = models.OneToOneField(RecipientJob, on_delete=models.PROTECT, related_name="message")
    message_id = models.CharField(max_length=255, unique=True)
    # Postfix's queue id from "250 2.0.0 Ok: queued as XXXX"; blank if the
    # server did not say.
    mta_queue_id = models.CharField(max_length=64, blank=True)
    envelope_from = models.CharField(max_length=254)
    content_hash = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    accepted_at = models.DateTimeField()

    class Meta:
        db_table = "delivery_message"
        indexes = [models.Index(fields=["mta_queue_id"])]
