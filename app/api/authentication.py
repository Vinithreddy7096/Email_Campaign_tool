"""Bearer-token authentication for API clients (§6.1 ApiToken)."""

from __future__ import annotations

from typing import Any

from rest_framework import authentication, exceptions
from rest_framework.request import Request

from accounts.models import ApiToken


class ApiTokenAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer ept_<prefix>_<secret>``.

    Sets ``request.auth`` to the ApiToken so views can check ``has_scope()``.
    A malformed, unknown, expired or revoked token gets one generic error.
    """

    keyword = "Bearer"

    def authenticate(self, request: Request) -> tuple[Any, ApiToken] | None:
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header")
        try:
            raw = header[1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid token") from exc
        token = ApiToken.authenticate(raw)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid token")
        return token.user, token

    def authenticate_header(self, request: Request) -> str:
        return self.keyword
