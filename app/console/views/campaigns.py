"""Campaigns: list, create/edit, detail with the whole workflow on one page."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from campaigns import services
from campaigns.models import Campaign
from console.forms import CampaignForm
from console.views.base import console_view, page, run
from delivery.models import RecipientJob

S = Campaign.Status
FILTERS = {
    "all": ("All", None),
    "review": ("Needs review", [S.READY_FOR_REVIEW, S.PREFLIGHT_FAILED, S.CHANGES_REQUESTED]),
    "live": ("Live", [S.SCHEDULED, S.SENDING, S.PAUSED]),
    "done": ("Done", [S.COMPLETED, S.CANCELED, S.REJECTED]),
}


@console_view
def campaign_list(request: HttpRequest) -> HttpResponse:
    current = request.GET.get("f", "all")
    query = request.GET.get("q", "").strip()
    base = Campaign.objects.select_related("sender_identity__domain", "template_version__template",
                                           "owner", "audience_list")  # fmt: skip
    if query:
        base = base.filter(name__icontains=query)
    tabs = []
    for key, (label, statuses) in FILTERS.items():
        qs = base if statuses is None else base.filter(status__in=statuses)
        tabs.append({"key": key, "label": label, "count": qs.count(), "current": key == current})
    statuses = FILTERS.get(current, FILTERS["all"])[1]
    rows = base if statuses is None else base.filter(status__in=statuses)
    return page(request, "console/campaigns.html", "campaigns", tabs=tabs, query=query,
                campaigns=rows.order_by("-created_at")[:200])  # fmt: skip


@console_view
def campaign_new(request: HttpRequest) -> HttpResponse:
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        created = run(request, lambda: services.create(request.user, **form.cleaned_data),
                      "Campaign created. Nothing sends until it is approved and scheduled.")  # fmt: skip
        if created:
            return redirect("console:campaign", pk=created.pk)
    return page(request, "console/campaign_form.html", "campaigns", form=form, campaign=None)


@console_view
def campaign_edit(request: HttpRequest, pk: str) -> HttpResponse:
    campaign = get_object_or_404(Campaign, pk=pk)
    if not campaign.is_editable:
        raise Http404("Only drafts can be edited")
    initial = {f: getattr(campaign, f) for f in services.EDITABLE_FIELDS}
    form = CampaignForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        changed = {k: form.cleaned_data[k] for k in form.changed_data}
        if run(request, lambda: services.update(request.user, campaign, **changed), "Draft saved."):
            return redirect("console:campaign", pk=campaign.pk)
    return page(request, "console/campaign_form.html", "campaigns", form=form, campaign=campaign)


def _steps(c: Campaign) -> list[dict[str, str]]:
    order = ["Setup", "Preflight", "Approval", "Schedule", "Sending", "Done"]
    reached = {
        S.DRAFT: 0, S.PREFLIGHT_FAILED: 1, S.CHANGES_REQUESTED: 1, S.READY_FOR_REVIEW: 2,
        S.REJECTED: 2, S.APPROVED: 3, S.SCHEDULED: 4, S.SENDING: 4, S.PAUSED: 4,
        S.COMPLETED: 6, S.CANCELED: 4,
    }[S(c.status)]  # fmt: skip
    failed = c.status in (S.PREFLIGHT_FAILED, S.REJECTED, S.CANCELED)
    out = []
    for i, label in enumerate(order):
        state = "done" if i < reached else ("fail" if failed else "now") if i == reached else ""
        out.append({"label": label, "state": state})
    return out


def _funnel(c: Campaign) -> list[dict[str, Any]] | None:
    audience = (c.preflight_report or {}).get("audience")
    if not audience:
        return None
    missing = sum(audience.get("missing_consent", {}).values())
    total = audience["total"] or 1
    return [
        {"label": "List members", "n": audience["total"], "tone": "idle", "of": total},
        {"label": "Suppressed", "n": audience["excluded_suppressed"], "tone": "warn", "of": total},
        {"label": "No consent", "n": missing, "tone": "bad", "of": total},
        {"label": "Eligible", "n": audience["recipients"], "tone": "ok", "of": total},
    ]


@console_view
def campaign_detail(request: HttpRequest, pk: str) -> HttpResponse:
    campaign = get_object_or_404(
        Campaign.objects.select_related("sender_identity__domain", "template_version__template",
                                        "audience_list", "audience_segment", "owner",
                                        "reviewed_by"), pk=pk)  # fmt: skip
    if request.method == "POST":
        return _act(request, campaign)
    snapshot = getattr(campaign, "snapshot", None)
    jobs = campaign.jobs.aggregate(
        total=Count("pk"),
        accepted=Count("pk", filter=Q(state=RecipientJob.State.ACCEPTED)),
        open=Count("pk", filter=~Q(state__in=RecipientJob.TERMINAL)),
        review=Count("pk", filter=Q(review_required=True)),
    )
    report = campaign.preflight_report or {}
    findings = report.get("findings", [])
    return page(request, "console/campaign_detail.html", "campaigns",
                c=campaign, snapshot=snapshot, steps=_steps(campaign), funnel=_funnel(campaign),
                findings=findings, samples=report.get("samples", []),
                fails=sum(f["level"] == "FAIL" for f in findings),
                warns=sum(f["level"] == "WARN" for f in findings),
                jobs=jobs, job_states=_job_states(campaign))  # fmt: skip


def _job_states(c: Campaign) -> list[tuple[str, int]]:
    rows = c.jobs.values("state").annotate(n=Count("pk")).order_by("-n")
    return [(r["state"], r["n"]) for r in rows]


def _act(request: HttpRequest, c: Campaign) -> HttpResponse:
    action = request.POST.get("action", "")
    note = request.POST.get("note", "")
    user = request.user
    when = None
    if action == "schedule" and request.POST.get("scheduled_at"):
        # An unparseable date must never fall through to "now".
        try:
            when = CampaignForm.base_fields["scheduled_at"].clean(request.POST["scheduled_at"])
        except ValidationError:
            messages.error(
                request, "That start time is not a valid date and time. Nothing was scheduled."
            )
            return redirect(reverse("console:campaign", args=[c.pk]))
    actions = {
        "submit": (lambda: services.submit(user, c), "Preflight passed. Ready for review."),
        "approve": (lambda: services.approve(user, c, note), "Approved. Snapshot frozen."),
        "request_changes": (lambda: services.request_changes(user, c, note), "Changes requested."),
        "reject": (lambda: services.reject(user, c, note), "Rejected."),
        "schedule": (lambda: services.schedule(user, c, when), "Scheduled."),
        "pause": (lambda: services.pause(user, c, note or f"paused from console by {user}"),
                  "Paused. " + services.ACCEPTED_STILL_COMPLETE),
        "resume": (lambda: services.resume(user, c), "Resumed."),
        "cancel": (lambda: services.cancel(user, c, note or f"canceled from console by {user}"),
                   "Canceled. " + services.ACCEPTED_STILL_COMPLETE),
    }  # fmt: skip
    if action in actions:
        func, done = actions[action]
        run(request, func, done)
    return redirect(reverse("console:campaign", args=[c.pk]))
