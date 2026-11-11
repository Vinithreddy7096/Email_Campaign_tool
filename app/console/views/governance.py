"""Audit log, unsubscribe page preview, login and the no-access page."""

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse

from audit.models import AuditEvent
from console.views.base import console_view, page

OBJECT_TYPES = [
    ("", "All objects"),
    ("email_templates.", "Template"),
    ("campaigns.", "Campaign"),
    ("domains.", "Domain"),
    ("suppression.", "Suppression"),
    ("accounts.", "User / token"),
    ("delivery.", "Delivery / pause"),
    ("contacts.", "Contact"),
]


@console_view
def audit(request: HttpRequest) -> HttpResponse:
    actor = request.GET.get("actor", "").strip()
    obj = request.GET.get("obj", "")
    events = AuditEvent.objects.select_related("actor").order_by("-created_at")
    if actor:
        events = events.filter(Q(actor__email__icontains=actor) | Q(actor_label__icontains=actor))
    if obj:
        events = events.filter(target_type__startswith=obj)
    paginator = Paginator(events, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    return page(request, "console/audit.html", "audit", page_obj=page_obj, actor=actor, obj=obj,
                object_types=OBJECT_TYPES)  # fmt: skip


@console_view
def unsubscribe_preview(request: HttpRequest) -> HttpResponse:
    return page(request, "console/unsubscribe_preview.html", "unsubscribe")


class LoginView(auth_views.LoginView):
    template_name = "console/login.html"
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    next_page = "console:login"  # a URL name; LogoutView resolves it
