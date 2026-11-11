"""Campaigns, approval snapshots (BUILD.md §10.6.1).

Status machine (§10.6.1):

    DRAFT ─submit─▶ PREFLIGHT_FAILED | READY_FOR_REVIEW
    READY_FOR_REVIEW ─▶ APPROVED | REJECTED | CHANGES_REQUESTED
    APPROVED ─schedule─▶ SCHEDULED ─dispatch─▶ SENDING ─▶ COMPLETED
    SCHEDULED/SENDING ─pause─▶ PAUSED ─resume─▶ (back)
    APPROVED/SCHEDULED/SENDING/PAUSED ─cancel─▶ CANCELED

Only DRAFT, PREFLIGHT_FAILED and CHANGES_REQUESTED are editable. Approval
freezes a CampaignSnapshot: what was approved, to whom, on what consent.
Dispatch (SCHEDULED → SENDING) belongs to the delivery pipeline (§10.7).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from common.models import AppendOnlyModel, NoDeleteModel, Stream


class Campaign(NoDeleteModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PREFLIGHT_FAILED = "PREFLIGHT_FAILED", "Preflight failed"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        SCHEDULED = "SCHEDULED", "Scheduled"
        SENDING = "SENDING", "Sending"
        COMPLETED = "COMPLETED", "Completed"
        PAUSED = "PAUSED", "Paused"
        CANCELED = "CANCELED", "Canceled"

    EDITABLE = (Status.DRAFT, Status.PREFLIGHT_FAILED, Status.CHANGES_REQUESTED)

    name = models.CharField(max_length=200)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    purpose = models.CharField(max_length=16, choices=Stream.choices)
    sender_identity = models.ForeignKey(
        "domains.SenderIdentity", on_delete=models.PROTECT, related_name="campaigns"
    )
    template_version = models.ForeignKey(
        "email_templates.TemplateVersion", on_delete=models.PROTECT, related_name="campaigns"
    )
    audience_list = models.ForeignKey(
        "contacts.ContactList", on_delete=models.PROTECT, related_name="campaigns"
    )
    # Optional narrowing of the list; must be a segment of audience_list.
    # The rule language is provisional (contacts.segments, Q-DATA-1).
    audience_segment = models.ForeignKey(
        "contacts.Segment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="campaigns",
    )  # fmt: skip
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    scheduled_at = models.DateTimeField(null=True, blank=True)

    preflight_report = models.JSONField(default=dict, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    # Set by pause(); resume() returns here and lifts exactly this SendPause.
    paused_from = models.CharField(max_length=20, blank=True)
    active_pause = models.ForeignKey(
        "delivery.SendPause", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    canceled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaigns_campaign"
        ordering = ["-created_at"]
        permissions = [
            ("submit_campaign", "Can submit campaigns for review (runs preflight)"),
            ("approve_campaign", "Can approve, reject or request changes to campaigns"),
            ("schedule_campaign", "Can schedule approved campaigns"),
            ("pause_campaign", "Can pause, resume and cancel campaigns"),
        ]
        constraints = [
            # A paused campaign always knows where to resume to, and nothing
            # else carries a resume target.
            models.CheckConstraint(
                condition=(
                    models.Q(status="PAUSED") & models.Q(paused_from__in=["SCHEDULED", "SENDING"])
                )
                | (~models.Q(status="PAUSED") & models.Q(paused_from="")),
                name="campaigns_campaign_pause_shape",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_editable(self) -> bool:
        return self.status in self.EDITABLE


class CampaignSnapshot(AppendOnlyModel):
    """What an approver approved, frozen (§10.6.1, spec §9 approve)."""

    campaign = models.OneToOneField(Campaign, on_delete=models.PROTECT, related_name="snapshot")
    template_version = models.ForeignKey(
        "email_templates.TemplateVersion", on_delete=models.PROTECT, related_name="+"
    )
    template_checksum = models.CharField(max_length=64)
    # from_address, display_name, reply_to, domain, sender_identity_id
    sender = models.JSONField()
    sending_domain = models.ForeignKey(
        "domains.SendingDomain", on_delete=models.PROTECT, related_name="+"
    )
    contact_list = models.ForeignKey(
        "contacts.ContactList", on_delete=models.PROTECT, related_name="+"
    )
    # The segment and its rules exactly as approved (segments can be edited later).
    segment = models.ForeignKey(
        "contacts.Segment", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    segment_definition = models.JSONField(null=True, blank=True)
    preflight_report = models.JSONField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    audience_total = models.PositiveIntegerField()
    excluded_suppressed = models.PositiveIntegerField()
    recipient_count = models.PositiveIntegerField()

    class Meta:
        db_table = "campaigns_snapshot"

    def __str__(self) -> str:
        return f"Snapshot of {self.campaign}"


class CampaignSnapshotRecipient(AppendOnlyModel):
    """One approved recipient and the consent record that justified them."""

    snapshot = models.ForeignKey(
        CampaignSnapshot, on_delete=models.PROTECT, related_name="recipients"
    )
    contact = models.ForeignKey("contacts.Contact", on_delete=models.PROTECT, related_name="+")
    email_hash = models.CharField(max_length=64)
    # NULL for transactional campaigns, which do not need marketing consent.
    consent_record = models.ForeignKey(
        "consent.ConsentRecord", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "campaigns_snapshot_recipient"
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "contact"], name="campaigns_snapshot_recipient_uniq"
            ),
        ]
