"""Contact import tasks. Logic lives in contacts.imports."""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

# A 100k-row file is parsed and written in one transaction; give it room.
IMPORT_TIME_LIMIT_S = 1800


@shared_task(name="contacts.process_import", time_limit=IMPORT_TIME_LIMIT_S,
             soft_time_limit=IMPORT_TIME_LIMIT_S - 60)  # fmt: skip
def process_import(import_id: str) -> str:
    from contacts import imports

    return imports.process(import_id).status


@shared_task(name="contacts.resume_imports")
def resume_imports() -> int:
    """Re-queue imports whose task was lost: never published (broker down at
    upload), or a worker died mid-run. ``process`` is idempotent and writes
    all-or-nothing, so running one twice is safe."""
    from contacts.models import ContactImport

    now = timezone.now()
    S = ContactImport.Status
    stalled = list(
        ContactImport.objects.filter(status=S.PENDING, updated_at__lt=now - timedelta(minutes=2))
        .values_list("pk", flat=True)
        .union(
            ContactImport.objects.filter(
                status__in=[S.VALIDATING, S.IMPORTING],
                updated_at__lt=now - timedelta(seconds=IMPORT_TIME_LIMIT_S + 120),
            ).values_list("pk", flat=True)
        )
    )
    for pk in stalled:
        process_import.delay(str(pk))
    return len(stalled)
