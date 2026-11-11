"""Delivery pipeline tasks (BUILD.md §10.7.2). Thin: logic is in
workers.services.delivery. Each stage enqueues the next only after its own
transaction has committed, so a worker never picks up a job before it exists."""

from __future__ import annotations

from functools import partial
from typing import Any

from celery import shared_task
from django.db import transaction

from workers.services import delivery


@shared_task(name="delivery.dispatch_due")
def dispatch_due() -> int:
    ids = delivery.dispatch_due_campaigns()
    for campaign_id in ids:
        transaction.on_commit(partial(expand.delay, str(campaign_id)))
    return len(ids)


@shared_task(name="delivery.expand")
def expand(campaign_id: str) -> int:
    job_ids = delivery.expand_campaign(campaign_id)
    for job_id in job_ids:
        render.delay(str(job_id))
    return len(job_ids)


@shared_task(name="delivery.render")
def render(job_id: str) -> str:
    job = delivery.render_job(job_id)
    if job.state == job.State.RENDERED:
        handoff.delay(job_id)
    return job.state


@shared_task(name="delivery.handoff")
def handoff(job_id: str) -> str:
    return delivery.handoff_job(job_id).state


@shared_task(name="delivery.sweep")
def sweep() -> dict[str, Any]:
    """Retries due, held jobs re-asked, lost render tasks re-sent, stuck
    submissions flagged, finished campaigns completed."""
    from common.metrics import heartbeat

    heartbeat("delivery.sweep")
    due = delivery.due_job_ids()
    for job_id in due:
        handoff.delay(str(job_id))
    stale = delivery.stale_queued_ids()
    for job_id in stale:
        render.delay(str(job_id))
    return {
        "handoff": len(due),
        "rerender": len(stale),
        "flagged": delivery.flag_stuck_submissions(),
        "completed": len(delivery.finalize_campaigns()),
    }
