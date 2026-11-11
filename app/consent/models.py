"""Append-only consent records and evidence (§6.4).

PROVISIONAL — written without the §6.4 field list; see docs/OPEN_QUESTIONS.md
Q-DATA-1.

Consent is never edited. A withdrawal is a new WITHDRAWN row; the current state
for a (contact, stream, list) is whichever row was recorded last. This is a data
lookup, not the send-time policy decision — that is the policy service (§10.5),
which must also check suppression and fail closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import AppendOnlyModel, AppendOnlyQuerySet, Stream
from contacts.models import Contact, ContactImport, ContactList


class ConsentQuerySet(AppendOnlyQuerySet):
    def latest_for(
        self, contact: Contact, stream: str, contact_list: ContactList | None = None
    ) -> ConsentRecord | None:
        # Timestamps can tie (coarse clocks, batch writes), and UUIDv7 ids give
        # no order within a millisecond. On a tie a withdrawal wins: fail closed.
        withdrawn_first = models.Case(
            models.When(action=ConsentRecord.Action.WITHDRAWN, then=0),
            default=1,
            output_field=models.IntegerField(),
        )
        latest = (
            self.filter(contact=contact, stream=stream, contact_list=contact_list)
            .order_by("-occurred_at", withdrawn_first)
            .first()
        )
        return cast("ConsentRecord | None", latest)

    def is_granted(
        self,
        contact: Contact,
        stream: str,
        contact_list: ContactList | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True if the latest record is a grant and, for marketing, is younger
        than CONSENT_MAX_AGE_DAYS when that ceiling is configured."""
        record = self.latest_for(contact, stream, contact_list)
        if record is None or record.action != ConsentRecord.Action.GRANTED:
            return False
        max_age = settings.CONSENT_MAX_AGE_DAYS
        if stream == Stream.MARKETING and max_age is not None:
            now = now or timezone.now()
            return record.occurred_at >= now - timedelta(days=max_age)
        return True


ConsentManager = models.Manager.from_queryset(ConsentQuerySet)


class ConsentRecord(AppendOnlyModel):
    class Action(models.TextChoices):
        GRANTED = "GRANTED", "Granted"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    class Method(models.TextChoices):
        WEB_FORM = "WEB_FORM", "Web form"
        DOUBLE_OPT_IN = "DOUBLE_OPT_IN", "Double opt-in confirmation"
        API = "API", "API"
        IMPORT = "IMPORT", "Attested import"
        UNSUBSCRIBE_LINK = "UNSUBSCRIBE_LINK", "Unsubscribe link"
        LIST_UNSUBSCRIBE = "LIST_UNSUBSCRIBE", "List-Unsubscribe header"
        MANUAL = "MANUAL", "Manual entry by staff"
        # Transactional basis. Which contracts qualify needs sign-off (Q-LEGAL-1).
        CONTRACT = "CONTRACT", "Contractual necessity"

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="consents")
    # Copied from the contact so evidence remains attributable after erasure.
    email_hash = models.CharField(max_length=64, editable=False)
    stream = models.CharField(max_length=16, choices=Stream.choices)
    # NULL = stream-wide; otherwise scoped to one list.
    contact_list = models.ForeignKey(
        ContactList, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    method = models.CharField(max_length=24, choices=Method.choices)
    occurred_at = models.DateTimeField(default=timezone.now)

    source = models.CharField(max_length=512, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    evidence_object_key = models.CharField(max_length=512, blank=True)
    source_import = models.ForeignKey(
        ContactImport, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    objects: ClassVar[ConsentManager] = ConsentManager()

    class Meta:
        db_table = "consent_record"
        indexes = [
            models.Index(fields=["contact", "stream", "contact_list", "-occurred_at"]),
            models.Index(fields=["email_hash"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(method="IMPORT") | models.Q(source_import__isnull=False),
                name="consent_record_import_requires_import",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.stream} via {self.method}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding and not self.email_hash:
            self.email_hash = self.contact.email_hash
        super().save(*args, **kwargs)
