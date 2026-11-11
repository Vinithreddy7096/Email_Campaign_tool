from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from common.admin import NoDeleteAdmin, ViewOnlyAdmin
from delivery.models import (
    Message,
    PolicyDecision,
    RecipientJob,
    RecipientJobTransition,
    SendAttempt,
    SendPause,
)


@admin.register(SendPause)
class SendPauseAdmin(NoDeleteAdmin):
    """Raise a pause here; it takes effect at the next policy decision. Pauses are
    never deleted — "Resume" records who lifted it and when."""

    list_display = ("__str__", "scope", "target", "reason", "created_by", "created_at", "lifted_at")
    list_filter = ("scope", ("lifted_at", admin.EmptyFieldListFilter))
    fields = ("scope", "target", "reason")
    actions = ["resume"]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None:
            return ()
        return (*self.fields, "created_by", "created_by_label", "lifted_at", "lifted_by")

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        if change:
            return  # existing pauses are read-only; use the Resume action
        row = SendPause.pause(obj.scope, obj.target, reason=obj.reason, by=request.user)
        obj.pk = row.pk

    @admin.action(description="Resume (lift the selected pauses)")
    def resume(self, request: HttpRequest, queryset: QuerySet[SendPause]) -> None:
        count = 0
        for pause in queryset.filter(lifted_at__isnull=True):
            pause.resume(by=request.user)
            count += 1
        self.message_user(request, f"Resumed {count} pause(s).", messages.SUCCESS)


@admin.register(PolicyDecision)
class PolicyDecisionAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "outcome", "purpose", "codes", "job_ref", "sending_domain")
    list_filter = ("outcome", "purpose")
    search_fields = ("job_ref", "email_hash")
    date_hierarchy = "created_at"

    @admin.display(description="Reasons")
    def codes(self, obj: PolicyDecision) -> str:
        return ", ".join(r.get("code", "?") for r in obj.reasons) or "—"


class TransitionInline(admin.TabularInline):
    model = RecipientJobTransition
    fields = ("created_at", "from_state", "to_state", "code", "detail", "actor")
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ("created_at",)

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class AttemptInline(admin.TabularInline):
    model = SendAttempt
    fields = ("number", "created_at", "outcome", "smtp_code", "smtp_response", "mta_host")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(RecipientJob)
class RecipientJobAdmin(ViewOnlyAdmin):
    """Read-only, except resolving jobs that need an operator (Q-DEL-1)."""

    list_display = ("recipient", "state", "purpose", "campaign", "attempts", "review_required",
                    "next_attempt_at", "updated_at")  # fmt: skip
    list_filter = ("state", "purpose", "review_required")
    search_fields = ("recipient", "email_hash", "idempotency_key")
    list_select_related = ("campaign",)
    inlines = [TransitionInline, AttemptInline]
    actions = ["mark_accepted", "cancel_jobs", "resend"]

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Needed for the review actions; every field stays read-only.
        return request.user.has_perm("delivery.change_recipientjob")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        return [f.name for f in RecipientJob._meta.fields]

    def _resolve(
        self, request: HttpRequest, queryset: QuerySet[RecipientJob], resolution: str
    ) -> None:
        from workers.services.delivery import InvalidJobTransition, resolve_review

        done = 0
        for job in queryset.filter(review_required=True):
            try:
                resolve_review(job.pk, resolution, actor=request.user, note="via admin")
                done += 1
            except InvalidJobTransition as exc:
                self.message_user(request, f"{job}: {exc}", messages.ERROR)
        skipped = queryset.filter(review_required=False).count()
        self.message_user(request, f"{resolution}: {done} job(s) resolved"
                          + (f"; {skipped} not awaiting review, skipped" if skipped else ""),
                          messages.SUCCESS)  # fmt: skip

    @admin.action(description="Review: the MTA log shows it was accepted")
    def mark_accepted(self, request: HttpRequest, queryset: QuerySet[RecipientJob]) -> None:
        self._resolve(request, queryset, "accepted")

    @admin.action(description="Review: cancel (do not send)")
    def cancel_jobs(self, request: HttpRequest, queryset: QuerySet[RecipientJob]) -> None:
        self._resolve(request, queryset, "cancel")

    @admin.action(description="Review: send again (risk of a duplicate)")
    def resend(self, request: HttpRequest, queryset: QuerySet[RecipientJob]) -> None:
        self._resolve(request, queryset, "resend")


@admin.register(Message)
class MessageAdmin(ViewOnlyAdmin):
    list_display = ("message_id", "mta_queue_id", "accepted_at", "size_bytes")
    search_fields = ("message_id", "mta_queue_id")
