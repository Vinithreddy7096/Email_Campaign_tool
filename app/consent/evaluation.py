"""Marketing consent: the one rule used by campaign preflight and the policy service.

For a contact and (optionally) a list, look at the latest stream-wide record
and the latest record for that list:

- either is a WITHDRAWN               → CONSENT_WITHDRAWN (any withdrawal wins)
- a GRANTED one within max age        → GRANTED (that record is the evidence)
- only GRANTED ones, all too old      → CONSENT_EXPIRED
- nothing on record                   → NO_CONSENT

"Latest" breaks timestamp ties in favour of a withdrawal (fail closed).
CONSENT_MAX_AGE_DAYS, when set, applies to marketing only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.models import Stream
from consent.models import ConsentRecord
from contacts.models import ContactList

GRANTED = "GRANTED"
CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
CONSENT_EXPIRED = "CONSENT_EXPIRED"
NO_CONSENT = "NO_CONSENT"

_CHUNK = 2000


@dataclass(frozen=True)
class MarketingConsent:
    code: str
    record_id: uuid.UUID | None = None  # the GRANTED record relied on

    @property
    def granted(self) -> bool:
        return self.code == GRANTED


def _latest_by_scope(
    contact_ids: list[Any], contact_list: ContactList | None
) -> dict[Any, list[dict[str, Any]]]:
    """Latest marketing record per (contact, scope), one query per chunk."""
    withdrawn_first = models.Case(
        models.When(action=ConsentRecord.Action.WITHDRAWN, then=0),
        default=1,
        output_field=models.IntegerField(),
    )
    scope = models.Q(contact_list__isnull=True)
    if contact_list is not None:
        scope |= models.Q(contact_list=contact_list)
    out: dict[Any, list[dict[str, Any]]] = {}
    for start in range(0, len(contact_ids), _CHUNK):
        rows = (
            ConsentRecord.objects.filter(
                scope, stream=Stream.MARKETING, contact_id__in=contact_ids[start : start + _CHUNK]
            )  # fmt: skip
            .order_by("contact_id", "contact_list_id", "-occurred_at", withdrawn_first)
            .distinct("contact_id", "contact_list_id")  # Postgres DISTINCT ON
            .values("contact_id", "id", "action", "occurred_at")
        )
        for row in rows:
            out.setdefault(row["contact_id"], []).append(dict(row))
    return out


def marketing_consent_bulk(
    contact_ids: Iterable[Any], contact_list: ContactList | None = None, *,
    now: datetime | None = None,
) -> dict[Any, MarketingConsent]:  # fmt: skip
    ids = list(contact_ids)
    now = now or timezone.now()
    max_age = settings.CONSENT_MAX_AGE_DAYS
    cutoff = now - timedelta(days=max_age) if max_age is not None else None
    latest = _latest_by_scope(ids, contact_list)

    result: dict[Any, MarketingConsent] = {}
    for contact_id in ids:
        rows = latest.get(contact_id, [])
        if any(r["action"] == ConsentRecord.Action.WITHDRAWN for r in rows):
            result[contact_id] = MarketingConsent(CONSENT_WITHDRAWN)
            continue
        fresh = [r for r in rows if cutoff is None or r["occurred_at"] >= cutoff]
        if fresh:
            best = max(fresh, key=lambda r: r["occurred_at"])
            result[contact_id] = MarketingConsent(GRANTED, best["id"])
        elif rows:
            result[contact_id] = MarketingConsent(CONSENT_EXPIRED)
        else:
            result[contact_id] = MarketingConsent(NO_CONSENT)
    return result


def marketing_consent(
    contact_id: Any, contact_list: ContactList | None = None, *, now: datetime | None = None
) -> MarketingConsent:
    return marketing_consent_bulk([contact_id], contact_list, now=now)[contact_id]
