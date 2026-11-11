"""MFA enrolment and verification pages."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from accounts import totp
from accounts.middleware import mark_verified
from accounts.models import User

PENDING_SECRET_KEY = "mfa_pending_secret"  # noqa: S105 — a session key name, not a secret


def _next_url(request: HttpRequest) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or "/"
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return "/"


def _go(name: str, next_url: str) -> HttpResponse:
    return redirect(f"{reverse(name)}?{urlencode({'next': next_url})}")


@login_required
def mfa_setup(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)  # @login_required guarantees a real user
    next_url = _next_url(request)
    if user.mfa_enabled:
        return _go("accounts:mfa-verify", next_url)

    # The secret lives only in the session until a valid code proves the user's
    # app has it; abandoning setup leaves nothing behind on the account.
    secret = request.session.get(PENDING_SECRET_KEY)
    if not secret:
        secret = request.session[PENDING_SECRET_KEY] = totp.generate_secret()

    error = None
    if request.method == "POST":
        if user.enroll_mfa(secret, request.POST.get("code", "")):
            request.session.pop(PENDING_SECRET_KEY, None)
            mark_verified(request)
            return redirect(next_url)
        error = "That code didn't match. Check your device's clock and try the current code."

    return render(
        request,
        "accounts/mfa_setup.html",
        {
            "secret_display": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            "otpauth_uri": totp.provisioning_uri(secret, user.email),
            "required": user.mfa_required,
            "error": error,
            "next": next_url,
        },
        status=400 if error else 200,
    )


@login_required
def mfa_verify(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)  # @login_required guarantees a real user
    next_url = _next_url(request)
    if not user.mfa_enabled:
        return _go("accounts:mfa-setup", next_url)

    error = None
    if request.method == "POST":
        if user.verify_mfa(request.POST.get("code", "")):
            mark_verified(request)
            return redirect(next_url)
        if user.is_locked:
            error = "Too many incorrect codes. This account is locked for 15 minutes."
        else:
            error = "That code is incorrect or has already been used."
    elif user.is_locked:
        error = "Too many incorrect codes. This account is locked for 15 minutes."

    return render(
        request,
        "accounts/mfa_verify.html",
        {"error": error, "next": next_url, "locked": user.is_locked},
        status=400 if error else 200,
    )
