"""Presentation helpers for the console: status → pill colour, number format."""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()

# One table so every page colours the same state the same way.
_TONE: dict[str, str] = {
    # campaigns
    "DRAFT": "", "PREFLIGHT_FAILED": "bad", "READY_FOR_REVIEW": "warn", "APPROVED": "ok",
    "REJECTED": "bad", "CHANGES_REQUESTED": "bad", "SCHEDULED": "", "SENDING": "info",
    "COMPLETED": "ok", "PAUSED": "warn", "CANCELED": "",
    # template versions
    "PENDING_APPROVAL": "warn", "RETIRED": "",
    # sending domains
    "ACTIVE": "ok", "VERIFIED": "info", "PENDING_VERIFICATION": "",
    # jobs
    "ELIGIBLE": "info", "QUEUED": "info", "RENDERED": "info", "SUBMITTED_TO_MTA": "info",
    "ACCEPTED": "ok", "DEFERRED": "warn", "BOUNCED": "bad", "EXPIRED": "bad", "SUPPRESSED": "",
    # DNS check outcomes
    "pass": "ok", "warn": "warn", "fail": "bad", "unknown": "",
    # suppression reasons
    "HARD_BOUNCE": "bad", "COMPLAINT": "bad", "SOFT_BOUNCE_LIMIT": "warn", "UNSUBSCRIBE": "",
    "MANUAL": "info", "ERASURE": "",
    # policy outcomes
    "SEND": "ok", "HOLD": "warn", "SUPPRESS": "",
    # contact imports (COMPLETED/REJECTED above)
    "PENDING": "", "VALIDATING": "info", "IMPORTING": "info", "FAILED": "bad",
}  # fmt: skip


# Where one status word means something different for another kind of thing.
_KIND_TONE: dict[str, dict[str, str]] = {
    # A paused domain is blocked (DNS broke, or an operator stopped it).
    "domain": {"PAUSED": "bad"},
}


def _tone_for(status: Any, kind: str = "") -> str:
    return _KIND_TONE.get(kind, {}).get(str(status), _TONE.get(str(status), ""))


@register.filter
def tone(status: Any) -> str:
    return _tone_for(status)


@register.filter
def num(value: Any) -> str:
    """12345 → 12,345. Non-numbers pass through."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "" if value is None else str(value)


@register.filter
def pct(part: Any, whole: Any) -> str:
    try:
        whole = float(whole)
        return f"{100 * float(part) / whole:.1f}%" if whole else "—"
    except (TypeError, ValueError):
        return "—"


@register.filter
def bar_width(part: Any, whole: Any) -> str:
    try:
        whole = float(whole)
        return f"{min(100.0, 100 * float(part) / whole):.1f}%" if whole else "0%"
    except (TypeError, ValueError):
        return "0%"


@register.simple_tag
def pill(status: Any, label: Any = None, kind: str = "") -> Any:
    from django.utils.html import format_html

    text = label if label not in (None, "") else str(status).replace("_", " ").capitalize()
    return format_html('<span class="pill {}">{}</span>', _tone_for(status, kind), text)


@register.filter
def timesince_fmt(delta: Any) -> str:
    """timedelta → "2 d 4 h", "2 m 40 s" (the two largest units)."""
    try:
        seconds = int(delta.total_seconds())
    except AttributeError:
        return ""
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size or (unit == "s" and not parts):
            parts.append(f"{seconds // size} {unit}")
            seconds %= size
    return " ".join(parts[:2])
