"""Today: telemetry for the current UTC day, what needs attention, queues."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

from django.db.models import Count, Min, Q
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone

from campaigns.models import Campaign
from common.models import Stream
from console.views.base import console_view, page
from console.views.deliverability import _dns_label
from delivery.models import RecipientJob, RecipientJobTransition
from domains.models import SendingDomain
from email_templates.models import TemplateVersion
from events.models import DeliveryEvent, EventType
from workers.services.events import rate_alerts

State = RecipientJob.State
OPEN = [State.QUEUED, State.RENDERED, State.DEFERRED, State.SUBMITTED_TO_MTA]


def _today_start() -> datetime:
    return datetime.combine(timezone.now().date(), time.min, tzinfo=UTC)


def _tiles(since: datetime) -> dict[str, Any]:
    moved = (RecipientJobTransition.objects.filter(created_at__gte=since)
             .values("to_state").annotate(n=Count("pk")))  # fmt: skip
    by_state = {row["to_state"]: row["n"] for row in moved}
    happened = (DeliveryEvent.objects.filter(occurred_at__gte=since)
                .values("type").annotate(n=Count("pk")))  # fmt: skip
    by_type = {row["type"]: row["n"] for row in happened}
    delivered = by_type.get(EventType.DELIVERED, 0)
    return {
        "scheduled": RecipientJob.objects.filter(created_at__gte=since).count(),
        "submitted": by_state.get(State.SUBMITTED_TO_MTA, 0),
        "accepted": by_state.get(State.ACCEPTED, 0),
        "delivered": delivered,
        "deferred": by_type.get(EventType.DEFERRED, 0),
        "hard_bounced": by_type.get(EventType.HARD_BOUNCE, 0),
        "complaints": by_type.get(EventType.COMPLAINT, 0),
        "outcomes": delivered + by_type.get(EventType.HARD_BOUNCE, 0) + by_type.get(EventType.SOFT_BOUNCE, 0),
        "suppressed": by_state.get(State.SUPPRESSED, 0),
        "unsubscribed": by_type.get(EventType.UNSUBSCRIBE, 0),
    }  # fmt: skip


def _attention() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for d in SendingDomain.objects.exclude(status__in=[SendingDomain.Status.ACTIVE,
                                                       SendingDomain.Status.RETIRED]):  # fmt: skip
        failing = [_dns_label(r["check"]) for r in d.last_dns_report if r["status"] == "fail"]
        items.append({
            "tone": "bad" if d.status == SendingDomain.Status.PAUSED else "info",
            "title": f"{d.domain}: {d.get_status_display()}",
            "detail": (f"DNS failing: {', '.join(failing)}. " if failing else "")
                      + "Campaigns on this domain cannot be scheduled; queued mail is held.",
            "url": reverse("console:domains") + f"?d={d.pk}", "cta": "Open domain",
        })  # fmt: skip
    items.append({
        "tone": "warn", "title": "No rate limits configured",
        "detail": "Policy checks 6 and 7 apply nothing until limits are set.", "q": "Q-POL-1",
        "url": reverse("console:controls"), "cta": "Review",
    })  # fmt: skip
    unknown = DeliveryEvent.objects.filter(type=EventType.UNKNOWN, review__isnull=True).count()
    if unknown:
        items.append({
            "tone": "warn", "title": f"{unknown} delivery report(s) could not be classified",
            "detail": "Stored raw; no suppression applied until someone classifies them.",
            "url": reverse("console:events"), "cta": "Review",
        })  # fmt: skip
    for alert in rate_alerts():
        items.append({
            "tone": "bad",
            "title": f"{alert['campaign'].name}: {alert['kind']} rate {100 * alert['rate']:.2f}%",
            "detail": f"{alert['count']} of {alert['base']} over the {100 * alert['limit']:.2f}% alert "
                      "threshold. Alert only — sending is not paused automatically.",
            "q": "Q-EVT-2", "url": reverse("console:campaign", args=[alert["campaign"].pk]), "cta": "Open",
        })  # fmt: skip
    review = RecipientJob.objects.filter(review_required=True).count()
    if review:
        items.append({
            "tone": "bad", "title": f"{review} message(s) may or may not have been sent",
            "detail": "Lost SMTP reply after DATA, or a render failed. Held, never resent automatically.",
            "q": "Q-DEL-1", "url": reverse("console:events"), "cta": "Review",
        })  # fmt: skip
    versions = list(TemplateVersion.objects.filter(status=TemplateVersion.Status.PENDING_APPROVAL)
                    .select_related("template")[:3])  # fmt: skip
    campaigns = list(Campaign.objects.filter(status=Campaign.Status.READY_FOR_REVIEW)[:3])
    if versions or campaigns:
        names = [str(v) for v in versions] + [c.name for c in campaigns]
        items.append({
            "tone": "info", "title": f"{len(names)} item(s) waiting for approval",
            "detail": ", ".join(names), "url": reverse("console:approvals"), "cta": "Open",
        })  # fmt: skip
    order = {"bad": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["tone"]])


def _queues(now: datetime) -> dict[str, Any]:
    open_jobs = RecipientJob.objects.filter(state__in=OPEN)
    oldest = open_jobs.aggregate(t=Min("created_at"))["t"]
    held = RecipientJob.objects.filter(state__in=[State.RENDERED, State.DEFERRED],
                                       next_attempt_at__gt=now)  # fmt: skip
    return {
        "depth": open_jobs.count(),
        "oldest": (now - oldest) if oldest else None,
        "held": held.filter(state=State.RENDERED).count(),
        "retrying": held.filter(state=State.DEFERRED).count(),
        "review": RecipientJob.objects.filter(review_required=True).count(),
    }


def _in_flight() -> list[dict[str, Any]]:
    rows = []
    live = (Campaign.objects.filter(status__in=[Campaign.Status.SCHEDULED, Campaign.Status.SENDING,
                                                Campaign.Status.PAUSED])
            .select_related("sender_identity__domain", "snapshot"))  # fmt: skip
    for c in live:
        counts = c.jobs.aggregate(
            accepted=Count("pk", filter=Q(state=State.ACCEPTED)),
            done=Count("pk", filter=Q(state__in=RecipientJob.TERMINAL)),
        )
        rows.append({"campaign": c, "audience": c.snapshot.recipient_count, **counts})
    return rows


def _transactional(since: datetime) -> dict[str, int]:
    jobs = RecipientJob.objects.filter(purpose=Stream.TRANSACTIONAL, created_at__gte=since)
    return jobs.aggregate(
        today=Count("pk"),
        accepted=Count("pk", filter=Q(state=State.ACCEPTED)),
        held=Count("pk", filter=Q(state__in=OPEN)),
    )


@console_view
def today(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    since = _today_start()
    return page(request, "console/today.html", "overview",
                since=since, now=now, tiles=_tiles(since), attention=_attention(),
                queues=_queues(now), in_flight=_in_flight(), transactional=_transactional(since))  # fmt: skip
