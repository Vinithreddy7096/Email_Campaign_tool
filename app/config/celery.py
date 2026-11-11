"""
Celery application (BUILD.md §9.2).

The settings in here are the ones that stop messages being silently lost:
late acks, requeue on worker loss, publisher confirms, prefetch 1, and a
dead-letter exchange per queue. DLQ depth > 0 is never normal.
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("email_platform")
app.config_from_object("django.conf:settings", namespace="CELERY")


def _queue(name: str) -> Queue:
    """A durable quorum queue that dead-letters to dlq.<name>."""
    return Queue(
        name,
        Exchange(name, type="direct", durable=True),
        routing_key=name,
        queue_arguments={
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": "dlx",
            "x-dead-letter-routing-key": f"dlq.{name}",
        },
    )


QUEUE_NAMES = ("expand", "render", "deliver", "events", "maintenance")

app.conf.update(
    task_queues=[_queue(n) for n in QUEUE_NAMES],
    # Nothing lands on `deliver` by accident.
    task_default_queue="maintenance",
    task_default_exchange="maintenance",
    task_default_routing_key="maintenance",
    # Ack after the work is done, not before.
    task_acks_late=True,
    # A SIGKILLed worker's in-flight task is requeued rather than lost.
    task_reject_on_worker_lost=True,
    # Required for the deliver queue; harmless elsewhere.
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=240,
    broker_transport_options={"confirm_publish": True},
    broker_connection_retry_on_startup=True,
    result_backend=None,  # state lives in Postgres, not in a result backend
    task_track_started=True,
    timezone="UTC",
    enable_utc=True,
    # Explicit: autodiscovery only finds a module literally named `tasks`.
    imports=("workers.tasks.domains", "workers.tasks.delivery", "workers.tasks.events",
             "workers.tasks.contacts"),
    # Each pipeline stage on its own queue (§9.2), so a slow MTA cannot starve
    # rendering or expansion.
    task_routes={
        "delivery.dispatch_due": {"queue": "expand"},
        "delivery.expand": {"queue": "expand"},
        "delivery.render": {"queue": "render"},
        "delivery.handoff": {"queue": "deliver"},
        "delivery.sweep": {"queue": "maintenance"},
        "events.normalize_payload": {"queue": "events"},
        "events.ingest_log_lines": {"queue": "events"},
        "events.recompute_stats": {"queue": "maintenance"},
        "contacts.process_import": {"queue": "maintenance"},
        "contacts.resume_imports": {"queue": "maintenance"},
    },
    beat_schedule={
        # DNS rarely changes, but a broken record must pause an ACTIVE domain
        # within hours, not days. Interval is provisional (Q-DATA-1).
        "dispatch-due-campaigns": {"task": "delivery.dispatch_due", "schedule": 60.0},
        "delivery-sweep": {"task": "delivery.sweep", "schedule": 60.0},
        # §10.8.3 per-campaign counters; also logs complaint/bounce-rate alerts.
        "events-recompute-stats": {"task": "events.recompute_stats", "schedule": 300.0},
        # Imports whose task was never published or whose worker died.
        "contacts-resume-imports": {"task": "contacts.resume_imports", "schedule": 300.0},
        "verify-sending-domains": {
            "task": "domains.verify_all",
            "schedule": crontab(minute=17, hour="*/6"),
        },
    },
)

app.autodiscover_tasks(["workers.tasks"])
