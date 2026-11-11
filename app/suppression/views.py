"""Public unsubscribe endpoint (BUILD.md §10.8.4, RFC 8058). No login.

GET  shows a one-button confirmation (link scanners must not unsubscribe people).
POST unsubscribes immediately — both the button and mailbox providers'
     one-click POST (body "List-Unsubscribe=One-Click"), which carries no CSRF
     token; hence csrf_exempt. The signed token is the authorization.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from suppression import unsubscribe


@csrf_exempt
@require_http_methods(["GET", "POST"])
def unsubscribe_view(request: HttpRequest, token: str) -> HttpResponse:
    parsed = unsubscribe.read_token(token)
    if parsed is None:
        return render(request, "suppression/unsubscribe.html", {"state": "invalid"}, status=400)
    if request.method == "GET":
        return render(request, "suppression/unsubscribe.html", {"state": "confirm"})

    one_click = request.POST.get("List-Unsubscribe") == "One-Click"
    contact = unsubscribe.apply(
        parsed,
        one_click=one_click,
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    if contact is None:
        return render(request, "suppression/unsubscribe.html", {"state": "invalid"}, status=400)
    if one_click:
        return HttpResponse("Unsubscribed.", content_type="text/plain")
    return render(request, "suppression/unsubscribe.html", {"state": "done"})
