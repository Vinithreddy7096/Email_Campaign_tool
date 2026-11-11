"""Policy checks 6–7 (BUILD.md §10.9): rate limits and warm-up. Mechanism only —
every number here is a test value; none are built in (Q-POL-1)."""

from datetime import datetime, time, timedelta

import pytest
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from campaigns import services as campaigns
from common.models import ImmutableRowError
from delivery.models import PolicyDecision, RateLimit, RecipientJob, SendPause, WarmupPlan
from tests.integration import test_delivery
from tests.integration.test_delivery import FakeSMTP, expand_and_render
from workers.services import delivery, ratelimit

pytestmark = [pytest.mark.integration, pytest.mark.django_db]
State = RecipientJob.State
HOUR = 3600

# The delivery suite's world: a campaign to three consenting recipients.
w = test_delivery.w
smtp = test_delivery.smtp


@pytest.fixture(autouse=True)
def local_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    from django.core.cache import cache

    cache.clear()
    return cache


def hand_off(ids, now=None):
    return [delivery.handoff_job(i, now=now, smtp_class=FakeSMTP) for i in ids]


def last_reasons(job):
    return (
        PolicyDecision.objects.filter(job_ref=str(job.pk)).order_by("-created_at").first().reasons
    )


def limit(w, scope="DOMAIN", target=None, n=2, per=HOUR, **kw):
    if target is None:
        target = str(w["domain"].pk) if scope == "DOMAIN" else ""
    return RateLimit.add(
        by=w["admin"], scope=scope, target=target, max_messages=n, per_seconds=per, **kw
    )


def plan(w, limits, started_on=None, **kw):
    return WarmupPlan.add(by=w["admin"], scope="DOMAIN", target=str(w["domain"].pk),
                          started_on=started_on or timezone.localdate(), daily_limits=limits, **kw)  # fmt: skip


# ─── Rate limits (check 6) ───────────────────────────────────────────────


def test_nothing_configured_limits_nothing(w):
    jobs = hand_off(expand_and_render(w))
    assert [j.state for j in jobs] == [State.ACCEPTED] * 3


def test_over_the_limit_is_held_with_a_retry_time(w):
    limit(w, n=2)
    now = timezone.now()
    jobs = hand_off(expand_and_render(w), now=now)
    assert sorted(j.state for j in jobs) == [State.ACCEPTED, State.ACCEPTED, State.RENDERED]
    held = next(j for j in jobs if j.state == State.RENDERED)
    reason = last_reasons(held)[0]
    assert (reason["check"], reason["code"]) == (6, "RATE_LIMITED")
    assert reason["retry_after_s"] > 0
    assert held.next_attempt_at == now + timedelta(seconds=reason["retry_after_s"])
    assert len(FakeSMTP.sessions) == 2


def test_backlog_gets_its_own_slots_not_one_stampede(w):
    """Held jobs are spread out at the limit's pace, not all retried at once."""
    limit(w, n=1, per=60)
    now = timezone.now()
    jobs = hand_off(expand_and_render(w), now=now)
    held = sorted(j.next_attempt_at for j in jobs if j.state == State.RENDERED)
    assert len(held) == 2 and held[1] - held[0] >= timedelta(seconds=50)


def test_held_mail_is_sent_once_the_window_moves_on(w):
    limit(w, n=2)
    now = timezone.now()
    ids = expand_and_render(w)
    hand_off(ids, now=now)
    later = now + timedelta(hours=2)
    jobs = hand_off(ids, now=later)
    assert [j.state for j in jobs] == [State.ACCEPTED] * 3


def test_a_hold_for_another_reason_uses_no_allowance(w, settings):
    limit(w, n=3, per=86400)
    ids = expand_and_render(w)
    pause = SendPause.pause("DOMAIN", str(w["domain"].pk), reason="checking", by=w["admin"])
    now = timezone.now()
    assert [j.state for j in hand_off(ids, now=now)] == [State.RENDERED] * 3
    pause.resume(by=w["admin"])
    later = now + timedelta(seconds=settings.DELIVERY_HOLD_RETRY_SECONDS + 1)
    assert [j.state for j in hand_off(ids, now=later)] == [State.ACCEPTED] * 3


def test_all_or_nothing_across_limits(w, local_cache):
    """A send refused by one limit takes nothing from the others."""
    loose = limit(w, n=100)
    limit(w, scope="CAMPAIGN", target=str(w["campaign"].pk), n=1)
    now = timezone.now()
    hand_off(expand_and_render(w), now=now)
    window = int(now.timestamp() // HOUR)
    assert local_cache.get(f"rate:{loose.pk}:{w['domain'].pk}:{window}") == 1


def test_a_limit_on_something_else_does_not_apply(w):
    limit(w, scope="CAMPAIGN", target="01900000-0000-7000-8000-000000000000", n=1)
    limit(
        w, scope="PROVIDER", target="gmail", n=1, recipient_domains=["gmail.com", "googlemail.com"]
    )
    assert [j.state for j in hand_off(expand_and_render(w))] == [State.ACCEPTED] * 3


def test_each_one_separately_and_per_provider(w):
    """Empty target: every recipient domain gets its own count. All three
    recipients are at example.net, so they share one."""
    limit(w, scope="PROVIDER", n=1)
    jobs = hand_off(expand_and_render(w))
    assert sorted(j.state for j in jobs) == [State.ACCEPTED, State.RENDERED, State.RENDERED]


def test_instance_wide_limit(w):
    limit(w, scope="TENANT", n=1)
    assert sorted(j.state for j in hand_off(expand_and_render(w))).count(State.ACCEPTED) == 1


def test_cache_failure_with_limits_configured_holds(w, monkeypatch):
    limit(w, n=100)

    def broken(*a, **k):
        raise ConnectionError("redis down")

    # Patch the backend class, not the `cache` proxy: the proxy forwards setattr to
    # whichever backend instance is current, and that instance is replaced when the
    # CACHES override is undone, so monkeypatch's own undo would hit a different object.
    monkeypatch.setattr(type(caches["default"]), "incr", broken)
    jobs = hand_off(expand_and_render(w))
    assert [j.state for j in jobs] == [State.RENDERED] * 3
    assert last_reasons(jobs[0])[0]["code"] == "POLICY_ERROR"


def test_cache_failure_without_limits_does_not_block(w, monkeypatch):
    def broken(*a, **k):
        raise ConnectionError("redis down")

    # Patch the backend class, not the `cache` proxy: the proxy forwards setattr to
    # whichever backend instance is current, and that instance is replaced when the
    # CACHES override is undone, so monkeypatch's own undo would hit a different object.
    monkeypatch.setattr(type(caches["default"]), "incr", broken)
    assert [j.state for j in hand_off(expand_and_render(w))] == [State.ACCEPTED] * 3


# ─── Warm-up (check 7) ───────────────────────────────────────────────────


def test_warmup_caps_the_day_and_waits_for_tomorrow(w):
    plan(w, [2, 10])
    now = timezone.now()
    jobs = hand_off(expand_and_render(w), now=now)
    held = [j for j in jobs if j.state == State.RENDERED]
    assert len(held) == 1
    assert last_reasons(held[0])[0]["code"] == "WARMUP_LIMIT"
    tomorrow = timezone.make_aware(
        datetime.combine(timezone.localdate(now) + timedelta(days=1), time.min)
    )
    assert held[0].next_attempt_at >= tomorrow


def test_warmup_not_started_sends_nothing_and_finished_limits_nothing(w):
    plan(w, [100], started_on=timezone.localdate() + timedelta(days=2))
    ids = expand_and_render(w)
    assert [j.state for j in hand_off(ids)] == [State.RENDERED] * 3
    assert all(
        j.next_attempt_at.date() >= timezone.localdate() + timedelta(days=2)
        for j in RecipientJob.objects.filter(pk__in=ids)
    )  # waits for day 1
    WarmupPlan.objects.update(started_on=timezone.localdate() - timedelta(days=5))  # finished
    RecipientJob.objects.filter(pk__in=ids).update(next_attempt_at=None)
    assert [j.state for j in hand_off(ids)] == [State.ACCEPTED] * 3


def test_warmup_survives_a_lost_cache(w, local_cache):
    """The day's count is re-seeded from the database, not reset to zero."""
    p = plan(w, [2])
    ids = expand_and_render(w)
    hand_off(ids[:2])
    local_cache.clear()
    assert hand_off(ids[2:])[0].state == State.RENDERED
    assert ratelimit.used_today(p) == 2


def test_provider_warmup_applies_only_to_that_provider(w):
    plan(w, [1], recipient_domains=["gmail.com"])
    assert [j.state for j in hand_off(expand_and_render(w))] == [State.ACCEPTED] * 3


def test_schedule_refuses_what_warmup_cannot_send_in_time(w, settings):
    settings.MESSAGE_MAX_AGE_HOURS = 24  # today + tomorrow at most
    plan(w, [1, 1, 50])
    with pytest.raises(campaigns.ScheduleRejected, match="Warm-up allows 2"):
        campaigns.schedule(w["admin"], w["campaign"], timezone.now())


def test_schedule_warns_when_nothing_is_configured(w):
    result = campaigns.schedule(w["admin"], w["campaign"], timezone.now())
    assert any("No rate limits" in m for m in result.warnings)
    assert any("No warm-up plan" in m for m in result.warnings)


# ─── Configuration ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kw", [
    {"scope": "TENANT", "target": "x"},
    {"scope": "PROVIDER", "target": "gmail"},  # a named provider needs its domains
    {"scope": "DOMAIN", "recipient_domains": ["gmail.com"]},
])  # fmt: skip
def test_bad_limits_are_refused(w, kw):
    with pytest.raises(ValidationError):
        RateLimit.add(by=w["admin"], max_messages=1, per_seconds=60, **{"target": "", **kw})


@pytest.mark.parametrize("limits", [[], [0], [5, -1], ["10"], [True]])
def test_bad_ramps_are_refused(w, limits):
    with pytest.raises(ValidationError):
        plan(w, limits)


def test_limits_are_audited_and_never_deleted(w):
    row = limit(w, n=5)
    row.disable(by=w["admin"])
    actions = list(
        AuditEvent.objects.filter(action__startswith="ratelimit.").values_list("action", flat=True)
    )
    assert actions == ["ratelimit.add", "ratelimit.disable"]
    with pytest.raises(ImmutableRowError), transaction.atomic():
        RateLimit.objects.filter(pk=row.pk).delete()
    assert RateLimit.objects.filter(pk=row.pk, is_active=False).exists()


def test_disabled_limit_no_longer_applies(w):
    limit(w, n=1).disable(by=w["admin"])
    assert [j.state for j in hand_off(expand_and_render(w))] == [State.ACCEPTED] * 3


# ─── Console ─────────────────────────────────────────────────────────────


def test_console_adds_shows_and_removes_limits(w, login_verified):
    client = login_verified(w["admin"])
    url = reverse("console:controls")
    assert "No limits are configured" in client.get(url).content.decode()
    resp = client.post(url, {"action": "add_limit", "scope": "DOMAIN", "target": f"DOMAIN:{w['domain'].pk}",
                             "max_messages": "500", "per": "3600", "note": "Q-POL-1 interim"})  # fmt: skip
    assert resp.status_code == 302
    row = RateLimit.objects.get()
    assert (row.scope, row.target, row.max_messages, row.per_seconds) == (
        "DOMAIN",
        str(w["domain"].pk),
        500,
        3600,
    )
    page = client.get(url).content.decode()
    assert "Domain news.example.com" in page and "No limits are configured" not in page
    client.post(url, {"action": "add_warmup", "target": f"DOMAIN:{w['domain'].pk}",
                      "started_on": timezone.localdate().isoformat(), "daily_limits": "50, 100, 200"})  # fmt: skip
    assert WarmupPlan.objects.get().daily_limits == [50, 100, 200]
    assert "day 1 of 3" in client.get(url).content.decode()
    client.post(url, {"action": "disable_limit", "limit": str(row.pk)})
    assert not RateLimit.objects.get().is_active


def test_console_refuses_a_target_of_the_wrong_kind(w, login_verified):
    client = login_verified(w["admin"])
    client.post(reverse("console:controls"), {"action": "add_limit", "scope": "CAMPAIGN",
                                              "target": f"DOMAIN:{w['domain'].pk}",
                                              "max_messages": "5", "per": "60"})  # fmt: skip
    assert not RateLimit.objects.exists()


def test_only_admins_change_limits(w, django_user_model, login_verified):
    from accounts.models import Role, UserRole

    analyst = django_user_model.objects.create_user("a@example.com", "pw", is_staff=True)
    UserRole.grant(analyst, Role.Code.ANALYST, by=None)
    client = login_verified(django_user_model.objects.get(pk=analyst.pk))
    client.post(reverse("console:controls"), {"action": "add_limit", "scope": "TENANT", "target": "",
                                              "max_messages": "5", "per": "60"})  # fmt: skip
    assert not RateLimit.objects.exists()
