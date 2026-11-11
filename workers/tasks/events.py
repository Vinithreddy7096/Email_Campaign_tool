"""Event pipeline tasks (BUILD.md §10.8). Thin: logic is in workers.services.events."""

from __future__ import annotations

import logging

from celery import shared_task

from events.models import EventSource, RawPayload
from workers.services import events

log = logging.getLogger(__name__)


@shared_task(name="events.normalize_payload")
def normalize_payload(payload_id: str) -> str:
    payload = RawPayload.objects.get(pk=payload_id)
    if payload.processed_at is not None:
        return "already processed"
    outcome: events.Outcome | None
    try:
        if payload.source == EventSource.WEBHOOK:
            outcome = events.normalize_webhook(payload)
        else:
            outcomes = events.ingest_mail(payload.body.encode("utf-8"))
            outcome = outcomes[0] if outcomes else None
    except Exception as exc:  # stored raw; an operator can look at it
        log.exception("could not normalize payload %s", payload_id)
        RawPayload.objects.filter(pk=payload.pk).update(error=repr(exc)[:2000])
        raise
    return outcome.event.type if outcome else "no event"


@shared_task(name="events.ingest_log_lines")
def ingest_log_lines(lines: list[str]) -> dict[str, int]:
    """The Postfix log tailer posts batches of lines here (§10.8.1)."""
    return events.ingest_log_lines(lines)


@shared_task(name="events.recompute_stats")
def recompute_stats() -> int:
    from common.metrics import heartbeat

    heartbeat("events.recompute_stats")
    n = events.recompute_campaign_stats()
    for alert in events.rate_alerts():
        # Q-EVT-2 default: alert only, never pause automatically.
        log.warning("campaign %s %s rate %.3f%% over %.3f%%", alert["campaign"].pk, alert["kind"],
                    100 * alert["rate"], 100 * alert["limit"])  # fmt: skip
    return n
