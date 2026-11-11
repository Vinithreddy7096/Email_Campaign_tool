"""Immutable audit log. INSERT+SELECT only (§6.10).

PROVISIONAL — written without the §6.10 field list; see docs/OPEN_QUESTIONS.md
Q-DATA-1. UPDATE and DELETE are blocked by triggers (migration 0002) as well as
by the model. TRUNCATE must be revoked from the app's role (see common.db).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models

from common.models import AppendOnlyModel


class AuditEvent(AppendOnlyModel):
    # PROTECT, not SET_NULL: nulling the actor would be an UPDATE, which the
    # trigger rejects. Users are deactivated, never deleted.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Who acted when there is no user: "system", "celery:<task>", "api:<token id>".
    actor_label = models.CharField(max_length=128, blank=True)
    action = models.CharField(max_length=128)
    target_type = models.CharField(max_length=128, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "audit_event"
        indexes = [
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["actor", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(actor__isnull=False) | ~models.Q(actor_label=""),
                name="audit_event_has_actor",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor_id or self.actor_label}"

    @classmethod
    def record(
        cls,
        action: str,
        *,
        actor: Any = None,
        actor_label: str = "",
        target: models.Model | None = None,
        before: Any = None,
        after: Any = None,
        **metadata: Any,
    ) -> AuditEvent:
        return cls.objects.create(
            action=action,
            actor=actor,
            actor_label=actor_label or ("" if actor else "system"),
            target_type=target._meta.label if target is not None else "",
            target_id=str(target.pk) if target is not None else "",
            before=before,
            after=after,
            metadata=metadata,
        )
