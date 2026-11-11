"""API views."""

from __future__ import annotations

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.models import ApiToken


@api_view(["GET"])
def whoami(request: Request) -> Response:
    """Who the caller is authenticated as, and with what. Lets a client verify a
    token without touching anything else."""
    token = request.auth if isinstance(request.auth, ApiToken) else None
    return Response(
        {
            "user": request.user.email,
            "roles": sorted(request.user.role_codes),
            "auth": "token" if token else "session",
            "scopes": token.scopes if token else None,
            "token_expires_at": token.expires_at if token else None,
        }
    )
