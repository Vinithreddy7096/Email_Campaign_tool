"""MFA enforcement for session (browser) logins.

A user who must use MFA — or who has turned it on — is held at the enrol or
verify page until they present a valid code in this session. Every login starts
unverified. API token requests are not affected: token auth happens inside the
DRF view, after this middleware, and a token is its own credential.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

SESSION_KEY = "mfa_verified_user"

# Paths a half-logged-in user must still reach: the MFA pages themselves, a
# way out, and things that are never user-specific — including unsubscribe
# links, which must work for anyone (an operator testing their own link too).
EXEMPT_PREFIXES = ("/account/mfa/", "/admin/logout/", "/logout/", "/health/", "/static/", "/u/")


def is_verified(request: HttpRequest) -> bool:
    return request.session.get(SESSION_KEY) == str(request.user.pk)


def mark_verified(request: HttpRequest) -> None:
    # New session id at the privilege boundary, against session fixation.
    request.session.cycle_key()
    request.session[SESSION_KEY] = str(request.user.pk)


def clear_mfa_on_login(sender: Any, request: HttpRequest, user: Any, **kwargs: Any) -> None:
    request.session.pop(SESSION_KEY, None)


def needs_challenge(request: HttpRequest) -> bool:
    if not settings.MFA_ENFORCED:
        return False  # local development only; production refuses to boot this way
    if not request.user.is_authenticated or is_verified(request):
        return False
    user: Any = request.user
    return bool(user.mfa_enabled or user.mfa_required)


class MfaEnforcementMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.path.startswith(EXEMPT_PREFIXES) and needs_challenge(request):
            if request.path.startswith("/api/"):
                return JsonResponse({"detail": "MFA verification required"}, status=403)
            enrolled = getattr(request.user, "mfa_enabled", False)
            target = "accounts:mfa-verify" if enrolled else "accounts:mfa-setup"
            return redirect(f"{reverse(target)}?{urlencode({'next': request.get_full_path()})}")
        return self.get_response(request)
