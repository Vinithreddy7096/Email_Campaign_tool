"""Global and scoped suppression. Rows are never deleted (§6.5).

PROVISIONAL — written without the §6.5 field list; see docs/OPEN_QUESTIONS.md
Q-DATA-1.

A row targets either one address (``email_hash``) or a whole recipient domain
(``domain``) — never both (BUILD.md §10.9 check 2: "address or domain").

Scopes:
- GLOBAL blocks every stream, including transactional (§8.2). It can never be
  lifted — a CHECK constraint forbids it, so no flag or override can.
- STREAM blocks one stream.
- LIST blocks one list.

STREAM and LIST rows can be lifted by a named user with a reason. Lifting
updates the row; it is never removed. ``blocks()`` is a lookup for the policy
service (workers/services/policy.py), not a substitute for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from audit.models import AuditEvent
from common.email import email_hash, normalize_domain, normalize_email
from common.models import NoDeleteModel, NoDeleteQuerySet, Stream
from contacts.models import ContactList


class SuppressionQuerySet(NoDeleteQuerySet):
    def active(self) -> SuppressionQuerySet:
        return self.filter(lifted_at__isnull=True)

    def blocking(
        self, raw_email: str, stream: str, contact_list: ContactList | None = None
    ) -> SuppressionQuerySet:
        scope = models.Q(scope=Suppression.Scope.GLOBAL) | models.Q(
            scope=Suppression.Scope.STREAM, stream=stream
        )
        if contact_list is not None:
            scope |= models.Q(scope=Suppression.Scope.LIST, contact_list=contact_list)
        return self.active().filter(scope).filter(self._target(raw_email))

    def blocking_for_send(
        self, raw_email: str, stream: str, contact_list: ContactList | None = None
    ) -> SuppressionQuerySet:
        """Rows that stop a send of this purpose — the policy's rule (send_scope)."""
        return (
            self.active().filter(send_scope(stream, contact_list)).filter(self._target(raw_email))
        )

    def blocked_among(
        self,
        recipients: Iterable[tuple[str, str]],
        stream: str,
        contact_list: ContactList | None = None,
    ) -> set[str]:
        """Bulk form of blocking_for_send: given (email_hash, domain) pairs,
        return the hashes a send of this purpose must exclude."""
        pairs = list(recipients)
        blocked: set[str] = set()
        for start in range(0, len(pairs), 2000):
            chunk = pairs[start : start + 2000]
            rows = (
                self.active()
                .filter(send_scope(stream, contact_list))
                .filter(
                    models.Q(email_hash__in={h for h, _ in chunk})
                    | models.Q(domain__in={d for _, d in chunk})
                )
                .values_list("email_hash", "domain")
            )
            hit_hashes = {h for h, _ in rows if h}
            hit_domains = {d for _, d in rows if d}
            blocked |= {h for h, d in chunk if h in hit_hashes or d in hit_domains}
        return blocked

    def for_address(self, raw_email: str) -> SuppressionQuerySet:
        """Every active row naming this address or its domain, whatever the scope."""
        return self.active().filter(self._target(raw_email))

    @staticmethod
    def _target(raw_email: str) -> models.Q:
        normalized = normalize_email(raw_email)
        return models.Q(email_hash=email_hash(normalized)) | models.Q(
            domain=normalized.rpartition("@")[2]
        )

    def blocks(self, raw_email: str, stream: str, contact_list: ContactList | None = None) -> bool:
        return self.blocking(raw_email, stream, contact_list).exists()


def send_scope(stream: str, contact_list: ContactList | None = None) -> models.Q:
    """Which suppression rows stop a send of this purpose (BUILD.md §10.9 check 2).

    Marketing: GLOBAL rows, marketing-stream rows and rows for this list.
    Transactional: every row — Q-POL-2's default is that all suppressions apply.
    """
    if stream != Stream.MARKETING:
        return models.Q()
    scope = models.Q(scope="GLOBAL") | models.Q(scope="STREAM", stream=Stream.MARKETING)
    if contact_list is not None:
        scope |= models.Q(scope="LIST", contact_list=contact_list)
    return scope


SuppressionManager = models.Manager.from_queryset(SuppressionQuerySet)


class Suppression(NoDeleteModel):
    class Scope(models.TextChoices):
        GLOBAL = "GLOBAL", "Global — all streams"
        STREAM = "STREAM", "One stream"
        LIST = "LIST", "One list"

    class Reason(models.TextChoices):
        HARD_BOUNCE = "HARD_BOUNCE", "Hard bounce"
        SOFT_BOUNCE_LIMIT = "SOFT_BOUNCE_LIMIT", "Soft-bounce threshold reached"
        COMPLAINT = "COMPLAINT", "Spam complaint"
        UNSUBSCRIBE = "UNSUBSCRIBE", "Unsubscribe"
        MANUAL = "MANUAL", "Manual"
        ERASURE = "ERASURE", "Data erasure request"

    # Exactly one of email_hash / domain is set (suppression_one_target).
    email_hash = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    # Blanked on erasure; the hash keeps the block in force.
    email = models.EmailField(max_length=254, null=True, blank=True)  # noqa: DJ001
    domain = models.CharField(max_length=253, null=True, blank=True)  # noqa: DJ001
    scope = models.CharField(max_length=8, choices=Scope.choices)
    # NULL unless scope is STREAM; the scope-shape CHECK depends on it.
    stream = models.CharField(  # noqa: DJ001
        max_length=16, choices=Stream.choices, null=True, blank=True
    )
    contact_list = models.ForeignKey(
        ContactList, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=24, choices=Reason.choices)
    # Free-form provenance: a DSN message id, FBL report id, form URL…
    source = models.CharField(max_length=512, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    lift_reason = models.TextField(blank=True)

    objects: ClassVar[SuppressionManager] = SuppressionManager()

    class Meta:
        db_table = "suppression"
        indexes = [models.Index(fields=["email_hash"]), models.Index(fields=["domain"])]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(email_hash__isnull=False) & models.Q(domain__isnull=True))
                | (models.Q(email_hash__isnull=True) & models.Q(domain__isnull=False)),
                name="suppression_one_target",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(scope="GLOBAL", stream__isnull=True, contact_list__isnull=True)
                    | models.Q(scope="STREAM", stream__isnull=False, contact_list__isnull=True)
                    | models.Q(scope="LIST", stream__isnull=True, contact_list__isnull=False)
                ),
                name="suppression_scope_shape",
            ),
            models.CheckConstraint(
                condition=~models.Q(scope="GLOBAL") | models.Q(lifted_at__isnull=True),
                name="suppression_global_never_lifted",
            ),
            models.CheckConstraint(
                condition=models.Q(lifted_at__isnull=True)
                | (models.Q(lifted_by__isnull=False) & ~models.Q(lift_reason="")),
                name="suppression_lift_is_attributed",
            ),
            models.UniqueConstraint(
                fields=["email_hash", "domain", "scope", "stream", "contact_list"],
                condition=models.Q(lifted_at__isnull=True),
                nulls_distinct=False,
                name="suppression_one_active_per_scope",
            ),
        ]

    def __str__(self) -> str:
        target = self.stream or self.contact_list_id or "all"
        who = self.email or (f"@{self.domain}" if self.domain else (self.email_hash or "")[:12])
        return f"{self.scope}:{target} {self.reason} {who}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.email:
            self.email = normalize_email(self.email)
            self.email_hash = email_hash(self.email)
        if self.domain:
            self.domain = normalize_domain(self.domain)
        super().save(*args, **kwargs)

    @classmethod
    def suppress(
        cls,
        raw_email: str,
        reason: str,
        *,
        scope: str = Scope.GLOBAL,
        stream: str | None = None,
        contact_list: ContactList | None = None,
        source: str = "",
        created_by: Any = None,
    ) -> tuple[Suppression, bool]:
        """Idempotent: returns the existing active row for this scope if any."""
        return cls._get_or_create_active(
            {"email_hash": email_hash(raw_email), "domain": None},
            {"email": raw_email},
            reason=reason,
            scope=scope,
            stream=stream,
            contact_list=contact_list,
            source=source,
            created_by=created_by,
        )

    @classmethod
    def suppress_hash(
        cls,
        hashed: str,
        reason: str,
        *,
        scope: str = Scope.GLOBAL,
        stream: str | None = None,
        source: str = "",
        created_by: Any = None,
    ) -> tuple[Suppression, bool]:
        """Suppress an address known only by its hash (bounces and complaints:
        the event log never stores the address). Matching is by hash anyway."""
        return cls._get_or_create_active(
            {"email_hash": hashed, "domain": None},
            {"email_hash": hashed},
            reason=reason,
            scope=scope,
            stream=stream,
            contact_list=None,
            source=source,
            created_by=created_by,
        )

    @classmethod
    def suppress_domain(
        cls,
        raw_domain: str,
        reason: str,
        *,
        scope: str = Scope.GLOBAL,
        stream: str | None = None,
        contact_list: ContactList | None = None,
        source: str = "",
        created_by: Any = None,
    ) -> tuple[Suppression, bool]:
        """Block every address at a recipient domain. Idempotent like suppress()."""
        domain = normalize_domain(raw_domain)
        return cls._get_or_create_active(
            {"email_hash": None, "domain": domain},
            {"domain": domain},
            reason=reason,
            scope=scope,
            stream=stream,
            contact_list=contact_list,
            source=source,
            created_by=created_by,
        )

    @classmethod
    def _get_or_create_active(
        cls,
        target: dict[str, Any],
        create_fields: dict[str, Any],
        *,
        reason: str,
        scope: str,
        stream: str | None,
        contact_list: ContactList | None,
        source: str,
        created_by: Any,
    ) -> tuple[Suppression, bool]:
        with transaction.atomic():
            existing = (
                cls.objects.active()
                .select_for_update()
                .filter(**target, scope=scope, stream=stream, contact_list=contact_list)
                .first()
            )
            if existing is not None:
                return existing, False
            row = cls.objects.create(
                **create_fields,
                scope=scope,
                stream=stream,
                contact_list=contact_list,
                reason=reason,
                source=source,
                created_by=created_by,
            )
            return row, True

    def lift(self, *, by: Any, reason: str) -> None:
        if self.scope == self.Scope.GLOBAL:
            raise PermissionError("GLOBAL suppressions are permanent and cannot be lifted")
        if by is None or not reason.strip():
            raise ValueError("lifting a suppression requires a user and a reason")
        if self.lifted_at is not None:
            return
        with transaction.atomic():
            self.lifted_at = timezone.now()
            self.lifted_by = by
            self.lift_reason = reason.strip()
            self.save(update_fields=["lifted_at", "lifted_by", "lift_reason", "updated_at"])
            AuditEvent.record("suppression.lift", actor=by, target=self, reason=self.lift_reason)
