"""Segment rules: a saved filter over one list's members.

PROVISIONAL — the spec names segments (§6.3) but gives no rule language
(docs/OPEN_QUESTIONS.md Q-DATA-1). This is the smallest language that covers
"which of this list's members": boolean combinations of conditions on contact
fields, free-form attributes, other-list membership and when consent was
granted. Reconcile with the spec when it arrives.

A segment only ever *narrows* its list. It never adds anyone and never
replaces the consent or suppression checks: campaign preflight and the policy
service still apply them to whoever the segment selects.

Shape::

    {}                                          every member of the list
    {"all": [node, ...]}                        AND
    {"any": [node, ...]}                        OR
    {"not": node}                               NOT
    {"field": F, "op": OP, "value": V}          one condition

Fields and operators:

    first_name, last_name       eq ne in not_in contains starts_with exists
    email_domain                eq ne in not_in
    attributes.<key>            eq ne in not_in contains starts_with exists gt gte lt lte
    created_at                  before after               (ISO date or datetime)
    consent_granted_at          before after               (a marketing GRANTED record in range)
    list                        in not_in                  (ContactList ids)

Text comparisons ignore case. ``exists`` takes ``true`` or ``false``.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from common.email import InvalidEmailError, normalize_domain

MAX_DEPTH = 6
MAX_CONDITIONS = 50
MAX_VALUES = 100
_ATTR_KEY = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_TEXT_OPS = {"eq", "ne", "in", "not_in", "contains", "starts_with", "exists"}
_ATTR_OPS = _TEXT_OPS | {"gt", "gte", "lt", "lte"}
_TIME_OPS = {"before", "after"}
_FIELD_OPS: dict[str, set[str]] = {
    "first_name": _TEXT_OPS,
    "last_name": _TEXT_OPS,
    "email_domain": {"eq", "ne", "in", "not_in"},
    "created_at": _TIME_OPS,
    "consent_granted_at": _TIME_OPS,
    "list": {"in", "not_in"},
}


class _Budget:
    def __init__(self) -> None:
        self.conditions = 0


def _fail(path: str, message: str) -> ValidationError:
    return ValidationError(f"Segment rule at {path}: {message}")


def to_q(definition: Any) -> Q:
    """Validate ``definition`` and return the Q that selects matching contacts.

    Raises ValidationError naming the first problem and where it is.
    """
    if definition in ({}, None):
        return Q()
    if not isinstance(definition, dict):
        raise _fail("$", "must be a JSON object.")
    return _node(definition, "$", 1, _Budget())


def validate(definition: Any) -> None:
    to_q(definition)


def _node(node: Any, path: str, depth: int, budget: _Budget) -> Q:
    if depth > MAX_DEPTH:
        raise _fail(path, f"nested deeper than {MAX_DEPTH} levels.")
    if not isinstance(node, dict) or not node:
        raise _fail(path, "must be a non-empty JSON object.")
    keys = set(node)
    if keys in ({"all"}, {"any"}):
        (combinator,) = keys
        children = node[combinator]
        if not isinstance(children, list) or not children:
            raise _fail(path, f'"{combinator}" needs a non-empty list.')
        parts = [
            _node(child, f"{path}.{combinator}[{i}]", depth + 1, budget)
            for i, child in enumerate(children)
        ]
        out = parts[0]
        for part in parts[1:]:
            out = (out & part) if combinator == "all" else (out | part)
        return out
    if keys == {"not"}:
        return ~_node(node["not"], f"{path}.not", depth + 1, budget)
    if keys == {"field", "op", "value"}:
        budget.conditions += 1
        if budget.conditions > MAX_CONDITIONS:
            raise _fail(path, f"more than {MAX_CONDITIONS} conditions.")
        return _condition(node["field"], node["op"], node["value"], path)
    raise _fail(path, 'expected {"all": …}, {"any": …}, {"not": …} or '
                      '{"field": …, "op": …, "value": …}.')  # fmt: skip


def _condition(field: Any, op: Any, value: Any, path: str) -> Q:
    if not isinstance(field, str) or not isinstance(op, str):
        raise _fail(path, '"field" and "op" must be strings.')
    if field.startswith("attributes."):
        key = field.partition(".")[2]
        if not _ATTR_KEY.match(key):
            raise _fail(path, f"attribute name {key!r} must be 1-64 letters, digits or _.")
        allowed = _ATTR_OPS
    elif field in _FIELD_OPS:
        allowed = _FIELD_OPS[field]
    else:
        known = ", ".join([*sorted(_FIELD_OPS), "attributes.<key>"])
        raise _fail(path, f"unknown field {field!r} (known: {known}).")
    if op not in allowed:
        raise _fail(path, f"{field} does not support {op!r} "
                          f"(use one of {', '.join(sorted(allowed))}).")  # fmt: skip

    if field in ("first_name", "last_name"):
        return _text(field, op, value, path)
    if field.startswith("attributes."):
        return _attribute(field.partition(".")[2], op, value, path)
    if field == "email_domain":
        return _email_domain(op, value, path)
    if field == "list":
        return _list(op, value, path)
    when = _moment(value, path)
    if field == "created_at":
        return Q(created_at__lt=when) if op == "before" else Q(created_at__gte=when)
    return _consent_granted(op, when)


# ─── Value helpers ───────────────────────────────────────────────────────


def _values(op: str, value: Any, path: str) -> list[Any]:
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            raise _fail(path, f'"{op}" needs a non-empty list.')
        if len(value) > MAX_VALUES:
            raise _fail(path, f'"{op}" takes at most {MAX_VALUES} values.')
        return value
    return [value]


def _scalar(value: Any, path: str) -> str | int | float | bool:
    if isinstance(value, (str, bool, int, float)):
        return value
    raise _fail(path, "values must be strings, numbers or true/false.")


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, '"exists" takes true or false.')
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "needs a non-empty string.")
    return value


def _any_of(parts: list[Q]) -> Q:
    out = parts[0]
    for part in parts[1:]:
        out |= part
    return out


def _moment(value: Any, path: str) -> datetime:
    if isinstance(value, str):
        parsed_dt = parse_datetime(value)
        if parsed_dt is not None:
            if timezone.is_naive(parsed_dt):
                parsed_dt = timezone.make_aware(parsed_dt)
            return parsed_dt
        parsed_d = parse_date(value)
        if isinstance(parsed_d, date):
            return timezone.make_aware(datetime.combine(parsed_d, time.min))
    raise _fail(path, "needs an ISO date (2026-01-31) or datetime.")


# ─── Conditions ──────────────────────────────────────────────────────────


def _text(column: str, op: str, value: Any, path: str) -> Q:
    if op == "exists":
        present = ~Q(**{column: ""})
        return present if _bool(value, path) else ~present
    if op == "contains":
        return Q(**{f"{column}__icontains": _string(value, path)})
    if op == "starts_with":
        return Q(**{f"{column}__istartswith": _string(value, path)})
    matches = _any_of(
        [Q(**{f"{column}__iexact": _string(v, path)}) for v in _values(op, value, path)]
    )
    return ~matches if op in ("ne", "not_in") else matches


def _attribute(key: str, op: str, value: Any, path: str) -> Q:
    has = Q(attributes__has_key=key)
    if op == "exists":
        return has if _bool(value, path) else ~has
    lookup = f"attributes__{key}"
    if op in ("gt", "gte", "lt", "lte"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(path, f'"{op}" needs a number.')
        return has & Q(**{f"{lookup}__{op}": value})
    if op == "contains":
        return has & Q(**{f"{lookup}__icontains": _string(value, path)})
    if op == "starts_with":
        return has & Q(**{f"{lookup}__istartswith": _string(value, path)})

    def one(v: Any) -> Q:
        v = _scalar(v, path)
        if isinstance(v, str):
            return Q(**{f"{lookup}__iexact": v})
        return Q(**{lookup: v})

    matches = has & _any_of([one(v) for v in _values(op, value, path)])
    # "ne"/"not_in" include contacts that lack the attribute altogether.
    return (~has | ~matches) if op in ("ne", "not_in") else matches


def _email_domain(op: str, value: Any, path: str) -> Q:
    parts = []
    for v in _values(op, value, path):
        try:
            domain = normalize_domain(_string(v, path).removeprefix("*").lstrip("@"))
        except InvalidEmailError as exc:
            raise _fail(path, str(exc)) from exc
        parts.append(Q(email__endswith=f"@{domain}"))
    matches = _any_of(parts)
    return ~matches if op in ("ne", "not_in") else matches


def _list(op: str, value: Any, path: str) -> Q:
    from contacts.models import ContactList, ListMembership

    ids = []
    for v in _values(op, value, path):
        try:
            ids.append(uuid.UUID(str(v)))
        except ValueError as exc:
            raise _fail(path, f"{v!r} is not a list id.") from exc
    found = set(ContactList.objects.filter(pk__in=ids).values_list("pk", flat=True))
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise _fail(path, f"no such list: {', '.join(missing)}.")
    members = ListMembership.objects.filter(contact_list_id__in=ids).values("contact_id")
    return ~Q(pk__in=members) if op == "not_in" else Q(pk__in=members)


def _consent_granted(op: str, when: datetime) -> Q:
    from common.models import Stream
    from consent.models import ConsentRecord

    window = {"occurred_at__lt": when} if op == "before" else {"occurred_at__gte": when}
    grants = ConsentRecord.objects.filter(
        stream=Stream.MARKETING, action=ConsentRecord.Action.GRANTED, **window
    ).values("contact_id")
    return Q(pk__in=grants)
