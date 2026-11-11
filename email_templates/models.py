"""Template / Version entities from spec §8 (BUILD.md §10.5.1)."""

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ImmutableVersion(Exception):
    """Raised when saving content changes to a version that is no longer a draft."""


class EmailTemplate(models.Model):
    class Category(models.TextChoices):
        TRANSACTIONAL = "transactional", "Transactional"
        MARKETING = "marketing", "Marketing"

    name = models.CharField(max_length=200, unique=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def latest_approved_version(self):
        return (
            self.versions.filter(status=TemplateVersion.Status.APPROVED).order_by("-number").first()
        )


def validate_variables(value):
    """{"name": {"required": bool, "default": <json>, "description": str}, ...}"""
    if not isinstance(value, dict):
        raise ValidationError("Variables must be an object keyed by variable name.")
    for name, spec in value.items():
        if not name.isidentifier() or name.startswith("_"):
            raise ValidationError(f"'{name}' is not a valid variable name.")
        if not isinstance(spec, dict):
            raise ValidationError(f"Variable '{name}' must be an object.")
        unknown = set(spec) - {"required", "default", "description"}
        if unknown:
            raise ValidationError(
                f"Variable '{name}' has unknown keys: {', '.join(sorted(unknown))}."
            )
        if "required" in spec and not isinstance(spec["required"], bool):
            raise ValidationError(f"Variable '{name}': required must be true or false.")


def validate_object(value):
    if not isinstance(value, dict):
        raise ValidationError("Must be a JSON object.")


class TemplateVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        RETIRED = "RETIRED", "Retired"

    CONTENT_FIELDS = ("subject", "html_body", "text_body", "variables", "sample_data")

    template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField(editable=False)
    subject = models.CharField(max_length=998)
    html_body = models.TextField("HTML body")
    text_body = models.TextField("Text body")
    variables = models.JSONField(default=dict, blank=True, validators=[validate_variables])
    sample_data = models.JSONField(default=dict, blank=True, validators=[validate_object])
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, editable=False
    )
    checksum = models.CharField(max_length=64, editable=False)
    preflight_report = models.JSONField(default=list, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    review_note = models.TextField(blank=True, editable=False)

    class Meta:
        ordering = ["template", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "number"], name="email_templates_version_number_unique"
            ),
        ]
        permissions = [
            ("submit_templateversion", "Can submit template versions for approval"),
            ("approve_templateversion", "Can approve, reject or retire template versions"),
        ]

    def __str__(self):
        return f"{self.template.name} v{self.number}"

    def save(self, *args, **kwargs):
        if self.pk:
            stored = (
                TemplateVersion.objects.filter(pk=self.pk)
                .values("status", *self.CONTENT_FIELDS)
                .first()
            )
            if (
                stored
                and stored["status"] != self.Status.DRAFT
                and any(stored[f] != getattr(self, f) for f in self.CONTENT_FIELDS)
            ):
                raise ImmutableVersion(
                    f"{self} is {stored['status']}; create a new version to change its content."
                )
        self.checksum = self.compute_checksum()
        super().save(*args, **kwargs)

    def content(self):
        return {field: getattr(self, field) for field in self.CONTENT_FIELDS}

    def compute_checksum(self):
        canonical = json.dumps(
            self.content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT
