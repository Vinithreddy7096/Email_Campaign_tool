"""
GET /metrics — Prometheus metrics about the mail pipeline (Bearer METRICS_TOKEN).

Business state, computed from the database at scrape time, so every web
instance reports the same numbers and nothing is lost on restart. Alert rules
in monitoring/prometheus/rules/ are written against these names.

Process-level metrics (requests, latency) are left to the reverse proxy;
RabbitMQ, Postgres, Redis, Postfix and the hosts have their own exporters.
"""

from __future__ import annotations

import contextlib
import hmac
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Min
from django.http import Http404, HttpRequest, HttpResponse
from django.utils import timezone
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

# Tasks that record a heartbeat (see heartbeat()); a stale one means beat or
# its worker stopped.
HEARTBEAT_TASKS = ("delivery.sweep", "events.recompute_stats")


def heartbeat(task: str) -> None:
    """Record that a periodic task ran. Never fails the task."""
    from django.core.cache import cache

    with contextlib.suppress(Exception):  # a metrics write must not break the task
        cache.set(f"heartbeat:{task}", time.time(), timeout=7 * 86400)


class PipelineCollector(Collector):
    def collect(self) -> Iterator[GaugeMetricFamily]:
        from delivery.models import (
            PolicyDecision,
            RateLimit,
            RecipientJob,
            SendAttempt,
            SendPause,
            WarmupPlan,
        )
        from domains.models import SendingDomain
        from events.models import DeliveryEvent, EventType

        now = timezone.now()
        hour_ago = now - timedelta(hours=1)
        state = RecipientJob.State

        yield _gauge("mailer_global_send_enabled", "1 if GLOBAL_SEND_ENABLED is true.",
                     int(bool(settings.GLOBAL_SEND_ENABLED)))  # fmt: skip
        yield _gauge("mailer_mock_mode", "1 if MOCK_MODE is true (everything goes to the sink).",
                     int(bool(settings.MOCK_MODE)))  # fmt: skip

        waiting = [state.QUEUED, state.RENDERED, state.DEFERRED, state.SUBMITTED_TO_MTA]
        counts = _counts(RecipientJob.objects.filter(state__in=waiting), "state")
        yield _labelled("mailer_jobs", "Recipient jobs not yet finished, by state.", "state",
                        {s: counts.get(s, 0) for s in waiting})  # fmt: skip
        yield _gauge("mailer_jobs_review_required", "Jobs flagged for an operator.",
                     RecipientJob.objects.filter(review_required=True)
                     .exclude(state__in=RecipientJob.TERMINAL).count())  # fmt: skip

        due = RecipientJob.objects.filter(state__in=[state.RENDERED, state.DEFERRED],
                                          review_required=False, next_attempt_at__lte=now)  # fmt: skip
        oldest_due = due.aggregate(t=Min("next_attempt_at"))["t"]
        yield _gauge("mailer_handoff_lag_seconds",
                     "How long the oldest job that is due for hand-off has been waiting.",
                     (now - oldest_due).total_seconds() if oldest_due else 0)  # fmt: skip
        oldest_queued = (RecipientJob.objects.filter(state=state.QUEUED)
                         .aggregate(t=Min("updated_at"))["t"])  # fmt: skip
        yield _gauge("mailer_render_lag_seconds", "Age of the oldest job waiting to be rendered.",
                     (now - oldest_queued).total_seconds() if oldest_queued else 0)  # fmt: skip

        attempts = _counts(SendAttempt.objects.filter(created_at__gte=hour_ago), "outcome")
        yield _labelled("mailer_send_attempts_last_hour", "SMTP hand-offs in the last hour, by outcome.",
                        "outcome", {o: attempts.get(o, 0) for o in SendAttempt.Outcome.values})  # fmt: skip

        decisions = _counts(PolicyDecision.objects.filter(created_at__gte=hour_ago), "outcome")
        yield _labelled("mailer_policy_decisions_last_hour", "Policy decisions in the last hour.",
                        "outcome", {o: decisions.get(o, 0) for o in PolicyDecision.Outcome.values})  # fmt: skip

        events = _counts(DeliveryEvent.objects.filter(occurred_at__gte=hour_ago), "type")
        yield _labelled("mailer_events_last_hour", "Delivery events in the last hour, by type.",
                        "type", {t: events.get(t, 0) for t in EventType.values})  # fmt: skip
        yield _gauge("mailer_events_unreviewed",
                     "Unrecognised bounce/complaint messages waiting for an operator.",
                     DeliveryEvent.objects.filter(type=EventType.UNKNOWN, review__isnull=True).count())  # fmt: skip

        pauses = _counts(SendPause.objects.active(), "scope")
        yield _labelled("mailer_pauses_active", "Active sending pauses, by scope.", "scope",
                        {s: pauses.get(s, 0) for s in SendPause.Scope.values})  # fmt: skip
        domains = _counts(SendingDomain.objects.all(), "status")
        yield _labelled("mailer_sending_domains", "Sending domains, by status.", "status",
                        {s: domains.get(s, 0) for s in SendingDomain.Status.values})  # fmt: skip

        yield _gauge("mailer_rate_limits_configured", "Active rate limits (0: Q-POL-1 unanswered).",
                     RateLimit.objects.filter(is_active=True).count())  # fmt: skip
        yield _gauge("mailer_warmup_plans_active", "Active warm-up plans.",
                     WarmupPlan.objects.filter(is_active=True).count())  # fmt: skip

        from workers.services.events import rate_alerts

        alerts = rate_alerts()
        yield _labelled("mailer_campaign_rate_alerts",
                        "Campaigns over the complaint / hard-bounce alert threshold.", "kind",
                        {k: sum(1 for a in alerts if a["kind"] == k) for k in ("complaint", "hard-bounce")})  # fmt: skip

        from django.core.cache import cache

        beats = GaugeMetricFamily("mailer_task_last_run_timestamp_seconds",
                                  "When a periodic task last ran (unix time).", labels=["task"])  # fmt: skip
        for task in HEARTBEAT_TASKS:
            try:
                value = cache.get(f"heartbeat:{task}")
            except Exception:
                value = None
            beats.add_metric([task], float(value or 0))
        yield beats


def _counts(qs: Any, field: str) -> dict[str, int]:
    return dict(qs.order_by().values_list(field).annotate(n=Count("pk")).values_list(field, "n"))


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, value=value)


def _labelled(name: str, doc: str, label: str, values: dict[str, Any]) -> GaugeMetricFamily:
    g = GaugeMetricFamily(name, doc, labels=[label])
    for key, value in values.items():
        g.add_metric([str(key)], float(value))
    return g


def metrics_view(request: HttpRequest) -> HttpResponse:
    token = settings.METRICS_TOKEN
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, supplied):
        raise Http404  # do not advertise the endpoint
    registry = CollectorRegistry()
    registry.register(PipelineCollector())
    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)
