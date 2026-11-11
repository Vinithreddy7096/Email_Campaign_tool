"""Event pipeline data (BUILD.md §10.8).

- RawPayload: a received document (provider webhook body, DSN or ARF email),
  kept as evidence. Never deleted; may be redacted for an erasure request.
- DeliveryEvent: one normalized fact (delivered, deferred, bounced,
  complained, unsubscribed, unknown). Append-only, idempotent on source_key.
  Stores an email hash, never the address — the table cannot be edited, so it
  must not hold personal data an erasure request would need to remove.
- EventReview: an operator's classification of an UNKNOWN event. Append-only;
  its effect is applied as a follow-up OPERATOR event.
- CampaignDeliveryStats: per-campaign counters (§10.8.3), recomputed from
  events by a periodic task.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from common.models import AppendOnlyModel, NoDeleteModel


class EventType(models.TextChoices):
    DELIVERED = "DELIVERED", "Delivered"
    DEFERRED = "DEFERRED", "Deferred (4xx)"
    HARD_BOUNCE = "HARD_BOUNCE", "Hard bounce"
    SOFT_BOUNCE = "SOFT_BOUNCE", "Soft bounce"
    COMPLAINT = "COMPLAINT", "Complaint"
    UNSUBSCRIBE = "UNSUBSCRIBE", "Unsubscribe"
    UNKNOWN = "UNKNOWN", "Unclassified"


class EventSource(models.TextChoices):
    POSTFIX_LOG = "POSTFIX_LOG", "Postfix delivery log"
    DSN = "DSN", "Bounce message (DSN)"
    FBL = "FBL", "Feedback loop (ARF)"
    WEBHOOK = "WEBHOOK", "Provider webhook"
    UNSUBSCRIBE_LINK = "UNSUBSCRIBE_LINK", "Unsubscribe link"
    OPERATOR = "OPERATOR", "Operator review"


class RawPayload(NoDeleteModel):
    source = models.CharField(max_length=16, choices=EventSource.choices)
    # Provider event id, or the email's Message-ID / content hash.
    external_id = models.CharField(max_length=255)
    body = models.TextField()
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    redacted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "events_raw_payload"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="events_raw_payload_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.external_id[:40]}"


class DeliveryEvent(AppendOnlyModel):
    type = models.CharField(max_length=16, choices=EventType.choices)
    source = models.CharField(max_length=16, choices=EventSource.choices)
    source_key = models.CharField(max_length=64, unique=True)
    occurred_at = models.DateTimeField()

    job = models.ForeignKey(
        "delivery.RecipientJob",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
    )
    campaign = models.ForeignKey(
        "campaigns.Campaign", on_delete=models.PROTECT, null=True, blank=True, related_name="events"
    )
    email_hash = models.CharField(max_length=64, blank=True)
    recipient_domain = models.CharField(max_length=253, blank=True)
    provider = models.CharField(max_length=64, blank=True)

    smtp_code = models.PositiveIntegerField(null=True, blank=True)
    enhanced_code = models.CharField(max_length=16, blank=True)
    # Diagnostic text with the recipient address removed.
    diagnostic = models.TextField(blank=True)
    raw_payload = models.ForeignKey(
        RawPayload, on_delete=models.PROTECT, null=True, blank=True, related_name="events"
    )
    suppression = models.ForeignKey(
        "suppression.Suppression", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "events_delivery_event"
        indexes = [
            models.Index(fields=["campaign", "type"]),
            models.Index(fields=["type", "-occurred_at"]),
            models.Index(fields=["email_hash", "type"]),
            models.Index(fields=["provider", "-occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.provider or self.recipient_domain} {self.enhanced_code}"


class EventReview(AppendOnlyModel):
    class Resolution(models.TextChoices):
        HARD_BOUNCE = "HARD_BOUNCE", "Hard bounce — suppress"
        SOFT_BOUNCE = "SOFT_BOUNCE", "Soft bounce — count only"
        COMPLAINT = "COMPLAINT", "Complaint — suppress marketing"
        IGNORE = "IGNORE", "Not a delivery report — ignore"

    event = models.OneToOneField(DeliveryEvent, on_delete=models.PROTECT, related_name="review")
    resolution = models.CharField(max_length=16, choices=Resolution.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    note = models.TextField(blank=True)

    class Meta:
        db_table = "events_review"


class CampaignDeliveryStats(models.Model):
    """Recomputed wholesale from DeliveryEvent; never edited by hand."""

    campaign = models.OneToOneField(
        "campaigns.Campaign",
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="delivery_stats",
    )
    delivered = models.PositiveIntegerField(default=0)
    deferred = models.PositiveIntegerField(default=0)
    hard_bounces = models.PositiveIntegerField(default=0)
    soft_bounces = models.PositiveIntegerField(default=0)
    complaints = models.PositiveIntegerField(default=0)
    unsubscribes = models.PositiveIntegerField(default=0)
    unknown = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "events_campaign_stats"

    def __str__(self) -> str:
        return f"Stats for {self.campaign_id}"
