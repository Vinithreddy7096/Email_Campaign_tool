"""Shared plumbing for console views: access, layout context, running actions.

Every console page needs a signed-in user who holds at least one platform role
(or is a superuser). MFA is enforced before any of this by the middleware.
Actions call the same service functions as the admin and the API, so
permission checks and audit records are identical everywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

SERVICE_ERRORS: tuple[type[BaseException], ...] = (PermissionDenied, ValidationError, ValueError)


def _service_errors() -> tuple[type[BaseException], ...]:
    """Every exception a service raises to refuse an action (not a crash)."""
    from campaigns import services as campaigns
    from email_templates import services as templates
    from workers.services.delivery import InvalidJobTransition

    return (
        *SERVICE_ERRORS,
        campaigns.InvalidTransition,
        campaigns.PreflightFailed,
        campaigns.ScheduleRejected,
        templates.InvalidTransition,
        templates.PreflightFailed,
        InvalidJobTransition,
    )


def console_view(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("console:login"))
        if not (user.is_superuser or user.role_codes):
            return render(request, "console/no_access.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper


def send_state() -> dict[str, str]:
    from delivery.models import SendPause

    if not settings.GLOBAL_SEND_ENABLED:
        return {"tone": "bad", "text": "Sending disabled",
                "detail": "GLOBAL_SEND_ENABLED=false — no worker hands off to an MTA"}  # fmt: skip
    if SendPause.objects.active().filter(scope=SendPause.Scope.GLOBAL).exists():
        return {"tone": "bad", "text": "All sending paused", "detail": "Emergency pause is on"}
    return {"tone": "ok", "text": "All sending running", "detail": ""}


def rail_counts() -> dict[str, int]:
    from campaigns.models import Campaign
    from delivery.models import RecipientJob
    from email_templates.models import EmailTemplate, TemplateVersion
    from events.models import DeliveryEvent, EventType

    pending_versions = TemplateVersion.objects.filter(
        status=TemplateVersion.Status.PENDING_APPROVAL
    )
    ready = Campaign.objects.filter(status=Campaign.Status.READY_FOR_REVIEW)
    return {
        "campaigns": Campaign.objects.exclude(status=Campaign.Status.CANCELED).count(),
        "templates": EmailTemplate.objects.filter(archived_at__isnull=True).count(),
        "approvals": pending_versions.count() + ready.count(),
        "review": RecipientJob.objects.filter(review_required=True).count()
        + DeliveryEvent.objects.filter(type=EventType.UNKNOWN, review__isnull=True).count(),
    }


ROLE_LABELS = {
    "SYSTEM_ADMIN": "System Administrator",
    "APPROVER": "Approver / Compliance",
    "CAMPAIGN_MANAGER": "Campaign Manager",
    "CONTENT_EDITOR": "Content Editor",
    "ANALYST": "Read-Only Analyst",
}


def page(request: HttpRequest, template: str, nav: str, **context: Any) -> HttpResponse:
    user = request.user
    roles = sorted(ROLE_LABELS.get(c, c) for c in user.role_codes)  # type: ignore[union-attr]
    if user.is_superuser and not roles:
        roles = ["Superuser"]
    return render(request, template, {
        "nav": nav,
        "send_state": send_state(),
        "counts": rail_counts(),
        "user_roles": roles,
        **context,
    })  # fmt: skip


def run(request: HttpRequest, action: Callable[[], Any], done: str) -> Any:
    """Run a service call; report success or the refusal as a toast message."""
    try:
        result = action()
    except _service_errors() as exc:
        text = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, text or exc.__class__.__name__)
        return None
    messages.success(request, done)
    for warning in getattr(result, "warnings", []) or []:
        messages.warning(request, warning)
    return result if result is not None else True
