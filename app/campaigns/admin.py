"""Campaign admin (BUILD.md §10.6). Every action goes through campaigns.services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from campaigns import services
from campaigns.models import Campaign, CampaignSnapshot
from common.admin import ViewOnlyAdmin

_ERRORS = (
    services.InvalidTransition,
    services.PreflightFailed,
    services.ScheduleRejected,
    PermissionDenied,
    ValidationError,
)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "purpose", "status", "owner", "scheduled_at", "reviewed_by")
    list_filter = ("status", "purpose")
    search_fields = ("name",)
    list_select_related = ("owner", "reviewed_by")
    actions = ["submit", "schedule", "pause", "resume", "cancel"]
    editable = ("name", "purpose", "sender_identity", "template_version", "audience_list",
                "audience_segment", "scheduled_at")  # fmt: skip
    info = ("review_link", "status", "owner", "submitted_by", "submitted_at", "reviewed_by",
            "reviewed_at", "review_note", "paused_from", "canceled_at", "created_at")  # fmt: skip
    fields = editable + info

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None or obj.is_editable:
            return self.info
        return self.editable + self.info

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False  # cancel instead; campaigns are referenced by sends

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        values = {
            name: form.cleaned_data[name] for name in self.editable if name in form.cleaned_data
        }
        if change:
            services.update(
                request.user, obj, **{k: values[k] for k in form.changed_data if k in values}
            )
        else:
            created = services.create(request.user, **values)
            obj.pk = created.pk

    @admin.display(description="Review")
    def review_link(self, obj: Campaign) -> str:
        if obj is None or obj.pk is None:
            return "-"
        url = reverse("admin:campaigns_campaign_review", args=[obj.pk])
        return format_html('<a href="{}">Preflight, samples and review</a>', url)

    def _run(self, request: HttpRequest, queryset: QuerySet[Campaign],
             func: Callable[[Campaign], Any], done: str) -> None:  # fmt: skip
        for campaign in queryset:
            try:
                result = func(campaign)
            except _ERRORS as exc:
                self.message_user(request, f"{campaign}: {exc}", messages.ERROR)
                continue
            self.message_user(request, f"{campaign}: {done}", messages.SUCCESS)
            for warning in getattr(result, "warnings", []):
                self.message_user(request, f"{campaign}: {warning}", messages.WARNING)

    @admin.action(description="Submit for review (runs preflight)")
    def submit(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        self._run(
            request, queryset, lambda c: services.submit(request.user, c), "ready for review."
        )

    @admin.action(description="Schedule (at the campaign's scheduled time, or now)")
    def schedule(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        self._run(request, queryset, lambda c: services.schedule(request.user, c), "scheduled.")

    @admin.action(description="Pause (stops future dispatch)")
    def pause(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        reason = f"paused in admin by {request.user}"
        self._run(request, queryset, lambda c: services.pause(request.user, c, reason),
                  f"paused. {services.ACCEPTED_STILL_COMPLETE}")  # fmt: skip

    @admin.action(description="Resume")
    def resume(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        self._run(request, queryset, lambda c: services.resume(request.user, c), "resumed.")

    @admin.action(description="Cancel (permanent)")
    def cancel(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        reason = f"canceled in admin by {request.user}"
        self._run(request, queryset, lambda c: services.cancel(request.user, c, reason),
                  f"canceled. {services.ACCEPTED_STILL_COMPLETE}")  # fmt: skip

    def get_urls(self) -> list[Any]:
        return [
            path(
                "<uuid:object_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="campaigns_campaign_review",
            ),  # fmt: skip
            *super().get_urls(),
        ]

    def review_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        campaign = get_object_or_404(
            Campaign.objects.select_related("sender_identity__domain", "template_version__template",
                                            "audience_list", "audience_segment"),
            pk=object_id,
        )  # fmt: skip
        if not self.has_view_or_change_permission(request, campaign):
            raise PermissionDenied
        can_review = (campaign.status == Campaign.Status.READY_FOR_REVIEW
                      and request.user.has_perm("campaigns.approve_campaign"))  # fmt: skip
        if request.method == "POST":
            if not can_review:
                raise PermissionDenied
            decision = request.POST.get("decision", "")
            note = request.POST.get("note", "")
            funcs = {"approve": services.approve, "reject": services.reject,
                     "request_changes": services.request_changes}  # fmt: skip
            func = funcs.get(decision)
            if func is None:
                self.message_user(request, "Unknown decision.", messages.ERROR)
            else:
                try:
                    func(request.user, campaign, note)
                except _ERRORS as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    self.message_user(request, f"{campaign}: {decision.replace('_', ' ')} recorded.",
                                      messages.SUCCESS)  # fmt: skip
            return redirect("admin:campaigns_campaign_review", object_id=campaign.pk)

        report = campaign.preflight_report or {}
        findings = report.get("findings", [])
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Review {campaign}",
            "campaign": campaign,
            "findings": findings,
            "has_failures": any(f["level"] == "FAIL" for f in findings),
            "audience": report.get("audience"),
            "samples": report.get("samples", []),
            "snapshot": CampaignSnapshot.objects.filter(campaign=campaign).first(),
            "can_review": can_review,
        }
        return TemplateResponse(request, "admin/campaigns/campaign/review.html", context)


@admin.register(CampaignSnapshot)
class CampaignSnapshotAdmin(ViewOnlyAdmin):
    list_display = (
        "campaign",
        "recipient_count",
        "excluded_suppressed",
        "approved_by",
        "created_at",
    )
    exclude = ("preflight_report",)
