"""
Abstract bases shared by every platform model (BUILD.md §6).

Append-only and no-delete rules are enforced twice: here, so application code
fails fast with a clear error, and by Postgres triggers (``common.db``), so raw
SQL, ``QuerySet.update()`` and future code paths cannot bypass them.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from common.uuid7 import uuid7


class Stream(models.TextChoices):
    """Mail streams are kept apart end to end — domains, pools, consent, suppression."""

    TRANSACTIONAL = "TRANSACTIONAL", "Transactional"
    MARKETING = "MARKETING", "Marketing"


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class TimestampedModel(UUIDModel):
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ImmutableRowError(Exception):
    """Raised on an attempt to change or remove a row the data model forbids changing."""


class NoDeleteQuerySet(models.QuerySet):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError(f"{self.model._meta.label} rows are never deleted")


class AppendOnlyQuerySet(NoDeleteQuerySet):
    def update(self, **kwargs: Any) -> int:
        raise ImmutableRowError(f"{self.model._meta.label} rows are append-only")

    def bulk_update(self, *args: Any, **kwargs: Any) -> int:
        raise ImmutableRowError(f"{self.model._meta.label} rows are append-only")


class NoDeleteModel(TimestampedModel):
    objects = NoDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError(f"{self._meta.label} rows are never deleted")


class AppendOnlyModel(UUIDModel):
    """INSERT and SELECT only. There is deliberately no ``updated_at``."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError(f"{self._meta.label} rows are append-only")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError(f"{self._meta.label} rows are append-only")
