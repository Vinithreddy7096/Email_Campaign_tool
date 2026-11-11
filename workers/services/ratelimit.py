"""
Policy checks 6–7 (BUILD.md §10.9): rate limits and warm-up.

The mechanism only — every number comes from RateLimit / WarmupPlan rows, and
with none configured nothing is limited (Q-POL-1). ``acquire`` runs only after
checks 1–5 pass, so a message held for any other reason never uses allowance.

Rate limits are a sliding window over two fixed windows in the cache (Redis):
smooth, so a limit of 600/hour lets one message through about every 6 s
instead of 600 at the top of each hour. A warm-up plan counts the day's
hand-offs; if the cache lost the day's counter it is re-seeded from
SendAttempt, so a Redis restart cannot reset a warm-up day to zero.

Allowance is taken for every bucket or for none. A send that is then
deferred or fails still counted — the limits err on the side of sending less.

Held jobs are not all told "try again when the window reopens": each takes a
numbered place in line for its bucket and is given its own slot
(window ÷ max apart), so a backlog drains at the limit's pace and each job
is re-decided about once rather than every window.
"""

from __future__ import annotations

import math
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from delivery.models import RateLimit, SendAttempt, WarmupPlan

DAY = 86400


@dataclass(frozen=True)
class Route:
    """What a send is counted against."""

    domain_id: str
    pool_id: str  # "" when the domain has no pool (MOCK_MODE)
    campaign_ref: str
    api_client_ref: str
    recipient_domain: str


@dataclass(frozen=True)
class Refusal:
    check: int  # 6 rate limit, 7 warm-up
    code: str
    detail: str
    retry_after_s: int


# ─── Which limits apply ──────────────────────────────────────────────────


def _subject(limit: RateLimit, route: Route) -> str | None:
    """What this limit counts for this send, or None if it does not apply."""
    s = RateLimit.Scope
    if limit.scope == s.PROVIDER:
        if limit.target:
            return limit.target if route.recipient_domain in limit.recipient_domains else None
        return route.recipient_domain
    by_scope: dict[str, str] = {s.TENANT: "all", s.CAMPAIGN: route.campaign_ref, s.DOMAIN: route.domain_id,
                                s.IP_POOL: route.pool_id, s.API_CLIENT: route.api_client_ref}  # fmt: skip
    value = by_scope.get(limit.scope, "")
    if not value or (limit.target and limit.target != value):
        return None
    return value


def _plans(route: Route) -> Iterator[WarmupPlan]:
    targets = Q(scope=WarmupPlan.Scope.DOMAIN, target=route.domain_id)
    if route.pool_id:
        targets |= Q(scope=WarmupPlan.Scope.IP_POOL, target=route.pool_id)
    for plan in WarmupPlan.objects.filter(targets, is_active=True).order_by("created_at"):
        if not plan.recipient_domains or route.recipient_domain in plan.recipient_domains:
            yield plan


# ─── Counting ────────────────────────────────────────────────────────────


def _rate_take(key: str, limit: int, window: int, now: float) -> tuple[bool, int]:
    """Count one send in a sliding window. (taken, seconds until there is room)."""
    index = int(now // window)
    elapsed = now - index * window
    current = f"{key}:{index}"
    cache.add(current, 0, timeout=window * 2 + 60)
    count = cache.incr(current)
    previous = cache.get(f"{key}:{index - 1}") or 0
    if previous * (1 - elapsed / window) + count <= limit:
        return True, 0
    cache.decr(current)
    room = limit - (count - 1)
    if room <= 0 or not previous:
        return False, max(1, math.ceil(window - elapsed))
    # previous * (1 - t/window) + (count - 1) + 1 <= limit  →  t ≥ window * (1 - (room-1)/previous)
    t = window * (1 - (room - 1) / previous)
    return False, max(1, math.ceil(t - elapsed))


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min))


def sent_on(plan: WarmupPlan, day: date) -> int:
    """Hand-offs the MTA accepted on this plan's route on ``day`` (from the database)."""
    start, end = _day_bounds(day)
    q = SendAttempt.objects.filter(outcome=SendAttempt.Outcome.ACCEPTED,
                                   created_at__gte=start, created_at__lt=end)  # fmt: skip
    if plan.scope == WarmupPlan.Scope.DOMAIN:
        q = q.filter(job__sender_identity__domain_id=uuid.UUID(plan.target))
    else:
        q = q.filter(job__sender_identity__domain__ip_pool_id=uuid.UUID(plan.target))
    if plan.recipient_domains:
        to = Q()
        for d in plan.recipient_domains:
            to |= Q(job__recipient__iendswith=f"@{d}")
        q = q.filter(to)
    return q.count()


def _warmup_key(plan: WarmupPlan, day: date) -> str:
    return f"warmup:{plan.pk}:{day.isoformat()}"


def used_today(plan: WarmupPlan, today: date | None = None) -> int:
    today = today or timezone.localdate()
    cached = cache.get(_warmup_key(plan, today))
    return int(cached) if cached is not None else sent_on(plan, today)


def _warmup_take(plan: WarmupPlan, day: date, limit: int) -> bool:
    key = _warmup_key(plan, day)
    cache.add(key, sent_on(plan, day), timeout=2 * DAY)
    if cache.incr(key) <= limit:
        return True
    cache.decr(key)
    return False


def _jitter(slot: float) -> float:
    """A little spread so equal slots on different buckets do not wake together,
    small enough that a job never drifts into the next job's slot."""
    return min(slot / 10, 30.0)


def _place_in_line(key: str, window: int, now: float) -> int:
    """This hold's position among holds on the same bucket in this window."""
    line = f"{key}:line:{int(now // window)}"
    cache.add(line, 0, timeout=window * 2 + 60)
    return int(cache.incr(line)) - 1


# ─── Entry point ─────────────────────────────────────────────────────────


def acquire(route: Route, *, now: datetime | None = None) -> list[Refusal]:
    """Take one message of allowance from every applicable limit, or none.

    Returns the refusals; empty means the send may go ahead. Raises if the
    cache is unreachable while limits are configured — the policy turns that
    into a HOLD (fail closed). With nothing configured the cache is not touched.
    """
    now = now or timezone.now()
    ts = now.timestamp()
    today = timezone.localdate(now)
    taken: list[str] = []  # counter keys to give back if any limit refuses
    refusals: list[Refusal] = []

    for limit in RateLimit.objects.filter(is_active=True).order_by("created_at"):
        subject = _subject(limit, route)
        if subject is None:
            continue
        key = f"rate:{limit.pk}:{subject}"
        ok, wait = _rate_take(key, limit.max_messages, limit.per_seconds, ts)
        if ok:
            taken.append(f"{key}:{int(ts // limit.per_seconds)}")
            continue
        slot = limit.per_seconds / limit.max_messages
        jitter = random.uniform(0, _jitter(slot))  # noqa: S311 — spreading retries, not security
        wait += math.ceil(_place_in_line(key, limit.per_seconds, ts) * slot + jitter)
        refusals.append(Refusal(6, "RATE_LIMITED", f"rate limit: {limit.describe()}", wait))

    for plan in _plans(route):
        allowed = plan.limit_on(today)
        if allowed is None:
            continue  # warm-up finished
        if allowed and _warmup_take(plan, today, allowed):
            taken.append(_warmup_key(plan, today))
            continue
        refusals.append(_warmup_refusal(plan, today, now))

    if refusals:
        for key in taken:
            cache.decr(key)
    return refusals


def _warmup_refusal(plan: WarmupPlan, today: date, now: datetime) -> Refusal:
    if plan.day_number(today) < 1:
        opens, detail = plan.started_on, f"warm-up starts {plan.started_on}: {plan.describe()}"
    else:
        opens = today + timedelta(days=1)
        detail = f"warm-up day {plan.day_number(today)} allowance used: {plan.describe()}"
    wait = (_day_bounds(opens)[0] - now).total_seconds()
    # Spread the backlog over the next day at that day's pace.
    next_limit = plan.limit_on(opens) or 0
    if next_limit:
        slot = DAY / next_limit
        jitter = random.uniform(0, _jitter(slot))  # noqa: S311 — spreading retries, not security
        wait += _place_in_line(f"warmup:{plan.pk}", DAY, now.timestamp()) * slot + jitter
    return Refusal(7, "WARMUP_LIMIT", detail, max(1, math.ceil(wait)))


# ─── For scheduling and the console ──────────────────────────────────────


def warmup_capacity(domain_id: str, pool_id: str, start: datetime, until: datetime) -> tuple[int | None, list[WarmupPlan]]:  # fmt: skip
    """How many messages the all-recipient warm-up plans on this route allow
    between ``start`` and ``until`` (None: no plan limits that period).
    Provider-specific plans are left out: they cap a share of the audience."""
    route = Route(domain_id, pool_id, "", "", "")
    plans = [p for p in _plans(route) if not p.recipient_domains]
    today = timezone.localdate()
    best: int | None = None
    for plan in plans:
        total, day = 0, timezone.localdate(start)
        while day <= timezone.localdate(until):
            allowed = plan.limit_on(day)
            if allowed is None:
                total = -1  # finished within the period: no cap from this plan
                break
            total += max(0, allowed - (used_today(plan, day) if day == today else 0))
            day += timedelta(days=1)
        if total >= 0:
            best = total if best is None else min(best, total)
    return best, plans
