"""Contacts and consent, domains, delivery events, suppression, sending controls."""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken
from campaigns.models import Campaign
from common.models import Stream
from console.forms import RateLimitForm, SuppressionForm, WarmupPlanForm
from console.views.base import console_view, page, run
from delivery.models import PolicyDecision, RateLimit, RecipientJob, SendPause, WarmupPlan
from domains.models import IpPool, SenderIdentity, SendingDomain
from suppression.models import Suppression
from workers.services import ratelimit

# ─── Domains and senders ─────────────────────────────────────────────────


def _dns_cells(d: SendingDomain) -> dict[str, Any]:
    by = {r["check"]: r for r in d.last_dns_report}
    ptr = [r for r in d.last_dns_report if r["check"].startswith("fcrdns:")]
    if ptr:
        worst = next((r for r in ptr if r["status"] == "fail"), ptr[0])
        by["ptr"] = {**worst, "detail": f"{len(ptr)} IP(s): " + worst["detail"]}
    return by


_DNS_LABELS = {"spf": "SPF", "dkim": "DKIM", "dmarc": "DMARC",
               "return_path_mx": "Return-path", "tracking": "Tracking"}  # fmt: skip


def _dns_label(check: str) -> str:
    if check.startswith("fcrdns:"):
        return f"PTR {check.split(':', 1)[1]}"
    return _DNS_LABELS.get(check, check)


@console_view
def domains(request: HttpRequest) -> HttpResponse:
    if request.method == "POST" and request.POST.get("action") == "recheck":
        d = get_object_or_404(SendingDomain, pk=request.POST.get("domain"))
        if not request.user.has_perm("domains.change_sendingdomain"):
            messages.error(request, "You need permission to change sending domains.")
        else:
            from workers.services.dns_checks import dnspython_resolver
            from workers.services.domain_verification import verify_domain

            run(request, lambda: verify_domain(d, dnspython_resolver(), actor=request.user),
                f"DNS re-checked for {d.domain}.")  # fmt: skip
        return redirect(reverse("console:domains") + f"?d={d.pk}")

    all_domains = list(SendingDomain.objects.select_related("ip_pool").order_by("domain"))
    rows = [{"d": d, "dns": _dns_cells(d)} for d in all_domains]
    selected = next((d for d in all_domains if str(d.pk) == request.GET.get("d")), None)
    if selected is None:
        selected = next((d for d in all_domains if d.status != SendingDomain.Status.ACTIVE),
                        all_domains[0] if all_domains else None)  # fmt: skip
    report = [
        {**r, "label": _dns_label(r["check"])}
        for r in (selected.last_dns_report if selected else [])
    ]
    return page(request, "console/domains.html", "domains", rows=rows, selected=selected,
                selected_report=report,
                senders=SenderIdentity.objects.select_related("domain").order_by("domain__domain"))  # fmt: skip


# ─── Bounces, complaints and delivery events ─────────────────────────────


RESOLUTION_BUTTONS = [
    ("HARD_BOUNCE", "Hard", "Hard bounce — suppress the address"),
    ("SOFT_BOUNCE", "Soft", "Soft bounce — count only"),
    ("COMPLAINT", "Complaint", "Complaint — suppress marketing"),
    ("IGNORE", "Ignore", "Not a delivery report — ignore"),
]


@console_view
def events(request: HttpRequest) -> HttpResponse:
    from events.models import DeliveryEvent, EventType

    if request.method == "POST":
        _events_action(request)
        return redirect("console:events")

    since = timezone.now() - timedelta(hours=24)
    by_provider = (DeliveryEvent.objects.filter(occurred_at__gte=since).exclude(provider="")
                   .values("provider").annotate(
                       delivered=Count("pk", filter=Q(type=EventType.DELIVERED)),
                       deferred=Count("pk", filter=Q(type=EventType.DEFERRED)),
                       hard=Count("pk", filter=Q(type=EventType.HARD_BOUNCE)),
                       soft=Count("pk", filter=Q(type=EventType.SOFT_BOUNCE)),
                       complaints=Count("pk", filter=Q(type=EventType.COMPLAINT)),
                       total=Count("pk")).order_by("-total"))  # fmt: skip
    unknown = (DeliveryEvent.objects.filter(type=EventType.UNKNOWN, review__isnull=True)
               .select_related("raw_payload", "job").order_by("-occurred_at")[:100])  # fmt: skip
    review = (RecipientJob.objects.filter(review_required=True)
              .select_related("campaign").order_by("updated_at")[:100])  # fmt: skip
    latest = DeliveryEvent.objects.select_related("campaign").order_by("-occurred_at")[:40]
    return page(request, "console/events.html", "events", by_provider=by_provider, unknown=unknown,
                review=review, latest=latest, resolutions=RESOLUTION_BUTTONS)  # fmt: skip


def _events_action(request: HttpRequest) -> None:
    if "resolution" in request.POST and "job" in request.POST:  # maybe-sent jobs (Q-DEL-1)
        from workers.services.delivery import RESOLUTIONS, resolve_review

        resolution = request.POST["resolution"]
        if resolution in RESOLUTIONS and request.user.has_perm("delivery.change_recipientjob"):
            run(request, lambda: resolve_review(request.POST["job"], resolution,
                                                actor=request.user, note="via console"),
                f"Resolved: {resolution}.")  # fmt: skip
        else:
            messages.error(request, "You need permission to resolve delivery reviews.")
        return
    if "classify" in request.POST:  # unclassified reports (§10.8.2)
        from events.models import DeliveryEvent
        from workers.services.events import resolve_unknown

        if not request.user.has_perm("events.add_eventreview"):
            messages.error(request, "You need permission to classify delivery reports.")
            return
        event = get_object_or_404(DeliveryEvent, pk=request.POST.get("event"))
        run(request, lambda: resolve_unknown(event, request.POST["classify"], actor=request.user,
                                             note=request.POST.get("note", "")),
            "Report classified.")  # fmt: skip


# ─── Suppression list ────────────────────────────────────────────────────

REASON_FILTERS = {
    "all": ("All", None),
    "bounce": ("Bounce", [Suppression.Reason.HARD_BOUNCE, Suppression.Reason.SOFT_BOUNCE_LIMIT]),
    "complaint": ("Complaint", [Suppression.Reason.COMPLAINT]),
    "unsub": ("Unsub", [Suppression.Reason.UNSUBSCRIBE]),
}


@console_view
def suppressions(request: HttpRequest) -> HttpResponse:
    form = SuppressionForm(request.POST or None)
    if request.method == "POST":
        if not request.user.has_perm("suppression.add_suppression"):
            messages.error(request, "You need permission to add suppressions.")
            return redirect("console:suppressions")
        if form.is_valid():
            d = form.cleaned_data
            scope: dict[str, Any] = (
                {"scope": Suppression.Scope.GLOBAL} if d["scope"] == "GLOBAL"
                else {"scope": Suppression.Scope.STREAM, "stream": Stream.MARKETING}
            )  # fmt: skip
            kwargs = {"source": d["source"], "created_by": request.user, **scope}
            target = d["target"]
            if "@" in target:
                act = lambda: Suppression.suppress(target, d["reason"], **kwargs)  # noqa: E731
            else:
                act = lambda: Suppression.suppress_domain(target, d["reason"], **kwargs)  # noqa: E731
            if run(request, act, f"{target} suppressed."):
                return redirect("console:suppressions")

    current = request.GET.get("f", "all")
    query = request.GET.get("q", "").strip().lower().removeprefix("*@")
    active = Suppression.objects.active()
    tiles = {
        "total": active.count(),
        "unsub": active.filter(reason=Suppression.Reason.UNSUBSCRIBE).count(),
        "bounce": active.filter(reason__in=REASON_FILTERS["bounce"][1]).count(),
        "complaint": active.filter(reason=Suppression.Reason.COMPLAINT).count(),
        "manual": active.filter(
            Q(reason=Suppression.Reason.MANUAL) | Q(domain__isnull=False)
        ).count(),
    }
    rows = active.select_related("contact_list", "created_by").order_by("-created_at")
    reasons = REASON_FILTERS.get(current, REASON_FILTERS["all"])[1]
    if reasons:
        rows = rows.filter(reason__in=reasons)
    if query:
        from common.email import InvalidEmailError, email_hash

        q = Q(domain__icontains=query) | Q(email__icontains=query)
        with contextlib.suppress(InvalidEmailError):
            q |= Q(email_hash=email_hash(query))  # finds erased addresses too
        rows = rows.filter(q)
    tabs = [
        {"key": k, "label": label, "current": k == current}
        for k, (label, _) in REASON_FILTERS.items()
    ]
    return page(request, "console/suppressions.html", "suppressions", form=form, rows=rows[:200],
                tiles=tiles, tabs=tabs, query=query)  # fmt: skip


# ─── Sending controls ────────────────────────────────────────────────────


@console_view
def controls(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        _control_action(request)
        return redirect("console:controls")

    active = list(SendPause.objects.active().select_related("created_by").order_by("-created_at"))
    paused = {(p.scope, p.target): p for p in active}
    global_pause = paused.get((SendPause.Scope.GLOBAL, ""))
    held = RecipientJob.objects.filter(state__in=[RecipientJob.State.QUEUED, RecipientJob.State.RENDERED,
                                                  RecipientJob.State.DEFERRED]).count()  # fmt: skip
    switches: list[dict[str, Any]] = []
    for stream, label in Stream.choices:
        switches.append({"scope": "STREAM", "target": stream, "name": f"Stream {label.lower()}",
                         "detail": "every domain and pool for this stream",
                         "pause": paused.get(("STREAM", stream))})  # fmt: skip
    for d in SendingDomain.objects.exclude(status=SendingDomain.Status.RETIRED).order_by("domain"):
        switches.append({"scope": "DOMAIN", "target": str(d.pk), "name": f"Domain {d.domain}",
                         "detail": f"{d.get_stream_display()} · {d.get_status_display()}",
                         "status": d.status, "pause": paused.get(("DOMAIN", str(d.pk)))})  # fmt: skip
    for pool in IpPool.objects.order_by("name"):
        switches.append({"scope": "IP_POOL", "target": str(pool.pk), "name": f"IP pool {pool.name}",
                         "detail": ", ".join(pool.ips.values_list("address", flat=True)) or "no IPs",
                         "pause": paused.get(("IP_POOL", str(pool.pk)))})  # fmt: skip
    for token in ApiToken.objects.filter(revoked_at__isnull=True).select_related("user"):
        switches.append({"scope": "API_CLIENT", "target": str(token.pk), "name": f"API client “{token.name}”",
                         "detail": token.user.email, "pause": paused.get(("API_CLIENT", str(token.pk)))})  # fmt: skip
    names = _target_names()
    limit_targets = [(label, [(f"{k}", n) for k, n in names.items() if k.startswith(scope + ":")])
                     for scope, label in (("CAMPAIGN", "Campaigns"), ("DOMAIN", "Sending domains"),
                                          ("IP_POOL", "IP pools"), ("API_CLIENT", "API clients"))]  # fmt: skip
    warmup_targets = [(k, n) for k, n in names.items() if k.split(":")[0] in ("DOMAIN", "IP_POOL")]
    limits = [{"row": r, "target": names.get(f"{r.scope}:{r.target}", r.target)}
              for r in RateLimit.objects.filter(is_active=True).order_by("scope", "created_at")]  # fmt: skip
    return page(request, "console/controls.html", "controls", global_pause=global_pause, held=held,
                switches=switches, active=active, limits=limits, plans=_plan_rows(names),
                limit_form=RateLimitForm(targets=[t for t in limit_targets if t[1]]),
                warmup_form=WarmupPlanForm(targets=warmup_targets,
                                           initial={"started_on": timezone.localdate()}),
                held_by_limits=_held_by_limits(),
                last_global=SendPause.objects.filter(scope="GLOBAL", lifted_at__isnull=False)
                .order_by("-lifted_at").first())  # fmt: skip


def _target_names() -> dict[str, str]:
    """Map "SCOPE:id" → a readable name, for every thing a limit can apply to."""
    names: dict[str, str] = {}
    live = Campaign.objects.exclude(
        status__in=[Campaign.Status.COMPLETED, Campaign.Status.CANCELED]
    )
    for c in live.order_by("name"):
        names[f"CAMPAIGN:{c.pk}"] = f"Campaign {c.name}"
    for d in SendingDomain.objects.exclude(status=SendingDomain.Status.RETIRED).order_by("domain"):
        names[f"DOMAIN:{d.pk}"] = f"Domain {d.domain}"
    for pool in IpPool.objects.order_by("name"):
        names[f"IP_POOL:{pool.pk}"] = f"IP pool {pool.name}"
    for t in ApiToken.objects.filter(revoked_at__isnull=True).order_by("name"):
        names[f"API_CLIENT:{t.pk}"] = f"API client “{t.name}”"
    return names


def _plan_rows(names: dict[str, str]) -> list[dict[str, Any]]:
    today = timezone.localdate()
    rows = []
    for plan in WarmupPlan.objects.filter(is_active=True).order_by("started_on"):
        day, allowed = plan.day_number(today), plan.limit_on(today)
        used: int | None = None
        if allowed:
            with contextlib.suppress(Exception):  # the cache is down: show the row anyway
                used = ratelimit.used_today(plan, today)
        state = ("not started" if day < 1 else "finished" if allowed is None
                 else f"day {day} of {len(plan.daily_limits)}")  # fmt: skip
        rows.append({"row": plan, "target": names.get(f"{plan.scope}:{plan.target}", plan.target),
                     "state": state, "allowed": allowed, "used": used,
                     "finished": allowed is None})  # fmt: skip
    return rows


def _held_by_limits() -> int:
    """Jobs waiting on checks 6–7 right now: held in the last day by a limit,
    and still waiting for their next attempt."""
    now = timezone.now()
    refs = set(PolicyDecision.objects.filter(outcome="HOLD", created_at__gte=now - timedelta(days=1),
                                             reasons__0__check__in=[6, 7])
               .values_list("job_ref", flat=True)[:100_000])  # fmt: skip
    return RecipientJob.objects.filter(
        pk__in=[r for r in refs if r], next_attempt_at__gt=now,
        state__in=[RecipientJob.State.RENDERED, RecipientJob.State.DEFERRED],
    ).count()  # fmt: skip


_ACTION_PERMS = {"add_limit": "delivery.add_ratelimit", "disable_limit": "delivery.change_ratelimit",
                 "add_warmup": "delivery.add_warmupplan", "disable_warmup": "delivery.change_warmupplan"}  # fmt: skip


def _control_action(request: HttpRequest) -> None:
    user = request.user
    action = request.POST.get("action", "")
    if not user.has_perm(_ACTION_PERMS.get(action, "delivery.add_sendpause")):
        messages.error(
            request, "You need the System Administrator role to change sending controls."
        )
        return
    if action == "pause":
        scope, target = request.POST.get("scope", ""), request.POST.get("target", "")
        reason = request.POST.get("reason", "").strip() or f"paused from console by {user}"
        if scope not in SendPause.Scope.values:
            messages.error(request, "Unknown pause scope.")
            return
        run(request, lambda: SendPause.pause(scope, target, reason=reason, by=user),
            "All sending paused." if scope == "GLOBAL" else "Paused.")  # fmt: skip
    elif action == "resume":
        pause = get_object_or_404(SendPause, pk=request.POST.get("pause"))
        run(request, lambda: pause.resume(by=user),
            "Sending resumed." if pause.scope == "GLOBAL" else "Resumed.")  # fmt: skip
    elif action in ("add_limit", "add_warmup"):
        _add_limit(request, action)
    elif action == "disable_limit":
        limit = get_object_or_404(RateLimit, pk=request.POST.get("limit"))
        run(request, lambda: limit.disable(by=user), "Limit removed; it no longer applies.")
    elif action == "disable_warmup":
        plan = get_object_or_404(WarmupPlan, pk=request.POST.get("plan"))
        run(request, lambda: plan.disable(by=user), "Warm-up plan removed.")


def _add_limit(request: HttpRequest, action: str) -> None:
    targets = list(_target_names().items())
    if action == "add_limit":
        form: Any = RateLimitForm(request.POST, targets=targets)
    else:
        form = WarmupPlanForm(request.POST, targets=targets)
    if not form.is_valid():
        errors = [e for errs in form.errors.values() for e in errs]
        messages.error(request, " ".join(errors))
        return
    d, user = form.cleaned_data, request.user
    if action == "add_limit":
        run(request, lambda: RateLimit.add(by=user, scope=d["scope"], target=d["target"],
                                           recipient_domains=d["recipient_domains"],
                                           max_messages=d["max_messages"], per_seconds=d["per"],
                                           note=d["note"]), "Rate limit added. It applies from the next hand-off.")  # fmt: skip
    else:
        run(request, lambda: WarmupPlan.add(by=user, scope=d["scope"], target=d["target"],
                                            started_on=d["started_on"], daily_limits=d["daily_limits"],
                                            recipient_domains=d["recipient_domains"], note=d["note"]),
            "Warm-up plan added.")  # fmt: skip
