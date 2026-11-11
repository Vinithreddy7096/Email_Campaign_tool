"""Adapters from self-contained components to the audit log.

email_templates calls ``func(actor=, action=, obj=, before=, after=)``
(BUILD.md §10.5.5 step 2). Its payloads can hold datetimes and other values
plain JSON cannot store, so they are normalized with Django's encoder first.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from audit.models import AuditEvent


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))


def record_component_event(
    *, actor: Any, action: str, obj: models.Model, before: Any = None, after: Any = None
) -> None:
    AuditEvent.record(
        action,
        actor=actor,
        target=obj,
        before=_jsonable(before),
        after=_jsonable(after),
    )
