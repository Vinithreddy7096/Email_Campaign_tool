"""Template context shared by every page."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def environment_banner(request: HttpRequest) -> dict[str, Any]:
    """Drives the [MOCK] banner required on every UI page (§5.3 item 5).

    Also exposes the kill-switch state so operators can see at a glance whether
    the platform is permitted to hand anything to an MTA.
    """
    return {
        "MOCK_MODE": settings.MOCK_MODE,
        "GLOBAL_SEND_ENABLED": settings.GLOBAL_SEND_ENABLED,
        "ENVIRONMENT": getattr(settings, "ENVIRONMENT", "unknown"),
    }
