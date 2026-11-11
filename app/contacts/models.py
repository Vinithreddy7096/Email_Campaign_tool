"""Contacts, lists, segments, imports (§6.3).

PROVISIONAL — written without the §6.3 field list; see docs/OPEN_QUESTIONS.md
Q-DATA-1.

Contacts are never hard-deleted. Erasure blanks the personal data and keeps
``email_hash`` so the address stays matchable against suppression (Q-LEGAL-1).
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from audit.models import AuditEvent
from common.email import email_hash, normalize_email
from common.models import NoDeleteModel, NoDeleteQuerySet, Stream, TimestampedModel


class ContactQuerySet(NoDeleteQuerySet):
    def live(self) -> ContactQuerySet:
        return self.filter(deleted_at__isnull=True)

    def by_email(self, raw: str) -> ContactQuerySet:
        return self.live().filter(email_hash=email_hash(raw))


ContactManager = models.Manager.from_queryset(ContactQuerySet)


class Contact(NoDeleteModel):
    # NULL only after erasure; see erase().
    email = models.EmailField(max_length=254, null=True, blank=True)  # noqa: DJ001
    email_hash = models.CharField(max_length=64, editable=False)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[ContactManager] = ContactManager()

    class Meta:
        db_table = "contacts_contact"
        indexes = [models.Index(fields=["email_hash"])]
        constraints = [
            models.UniqueConstraint(
                fields=["email_hash"],
                condition=models.Q(deleted_at__isnull=True),
                name="contacts_contact_live_email_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(email__isnull=False) | models.Q(deleted_at__isnull=False),
                name="contacts_contact_email_unless_erased",
            ),
        ]

    def __str__(self) -> str:
        return self.email or f"<erased {self.email_hash[:12]}>"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.email:
            self.email = normalize_email(self.email)
            self.email_hash = email_hash(self.email)
        super().save(*args, **kwargs)

    def erase(self, *, by: Any = None) -> None:
        """GDPR erasure. Keeps the row and email_hash; drops everything personal."""
        with transaction.atomic():
            self.email = None
            self.first_name = ""
            self.last_name = ""
            self.attributes = {}
            self.deleted_at = self.deleted_at or timezone.now()
            self.save()
            AuditEvent.record("contact.erase", actor=by, target=self)


class ContactList(TimestampedModel):
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    stream = models.CharField(max_length=16, choices=Stream.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contacts_list"

    def __str__(self) -> str:
        return self.name


class ContactImport(TimestampedModel):
    """An uploaded file of contacts.

    Every import carries a named attester and a statement of where consent came
    from. Purchased, scraped, harvested or rented lists are prohibited (§0.2);
    the attestation is the record that someone declared this list is not one.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VALIDATING = "VALIDATING", "Validating"
        IMPORTING = "IMPORTING", "Importing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        REJECTED = "REJECTED", "Rejected"

    contact_list = models.ForeignKey(ContactList, on_delete=models.PROTECT, related_name="imports")
    file_object_key = models.CharField(max_length=512)
    original_filename = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    consent_source = models.TextField()
    attestation_text = models.TextField()
    attested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    attested_at = models.DateTimeField()

    total_rows = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    skipped_suppressed = models.PositiveIntegerField(default=0)
    skipped_invalid = models.PositiveIntegerField(default=0)
    skipped_duplicate = models.PositiveIntegerField(default=0)
    # Withdrew marketing consent earlier, or were erased (GDPR): never re-added by a file.
    skipped_opted_out = models.PositiveIntegerField(default=0)
    # Up to ISSUE_LIMIT {"row", "code", "detail"} entries. Row numbers, never addresses.
    issues = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    ISSUE_LIMIT = 200
    FINISHED = (Status.COMPLETED, Status.FAILED, Status.REJECTED)

    class Meta:
        db_table = "contacts_import"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(consent_source="") & ~models.Q(attestation_text=""),
                name="contacts_import_requires_attestation",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.original_filename} → {self.contact_list}"

    @property
    def is_finished(self) -> bool:
        return self.status in self.FINISHED


class ContactImportFile(NoDeleteModel):
    """The uploaded bytes, kept as evidence of what was attested.

    Stored in Postgres rather than object storage: app containers are read-only
    with no shared volume, and an import is at most CONTACT_IMPORT_MAX_BYTES.
    It holds personal data, so like RawPayload it is never deleted but can be
    redacted; retention is Q-LEGAL-1.
    """

    contact_import = models.OneToOneField(
        ContactImport, on_delete=models.PROTECT, related_name="file"
    )
    content = models.BinaryField()
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    redacted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contacts_import_file"

    def __str__(self) -> str:
        return f"{self.contact_import.original_filename} ({self.size} bytes)"

    def redact(self, *, by: Any) -> None:
        if self.redacted_at:
            return
        with transaction.atomic():
            self.content = b""
            self.redacted_at = timezone.now()
            self.save(update_fields=["content", "redacted_at", "updated_at"])
            AuditEvent.record("contact_import.redact_file", actor=by, target=self.contact_import)


class ListMembership(TimestampedModel):
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="memberships")
    contact_list = models.ForeignKey(
        ContactList, on_delete=models.PROTECT, related_name="memberships"
    )
    source_import = models.ForeignKey(
        ContactImport, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "contacts_list_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "contact_list"], name="contacts_list_membership_uniq"
            ),
        ]


class Segment(TimestampedModel):
    """A saved filter over one list's members. The rule language is provisional
    (Q-DATA-1) and lives in ``contacts.segments``; ``{}`` selects every member."""

    contact_list = models.ForeignKey(ContactList, on_delete=models.PROTECT, related_name="segments")
    name = models.CharField(max_length=200)
    definition = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "contacts_segment"
        constraints = [
            models.UniqueConstraint(
                fields=["contact_list", "name"], name="contacts_segment_name_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.contact_list} / {self.name}"

    def clean(self) -> None:
        from contacts import segments

        try:
            segments.validate(self.definition)
        except ValidationError as exc:
            raise ValidationError({"definition": exc.messages}) from exc

    def members(self) -> ContactQuerySet:
        """Live members of the list that match the rules (validated first)."""
        from contacts import segments

        return (
            Contact.objects.live()
            .filter(memberships__contact_list=self.contact_list_id)
            .filter(segments.to_q(self.definition))
        )
