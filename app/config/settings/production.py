"""
Production. Real IPs, real recipients, real reputation.

GLOBAL_SEND_ENABLED still defaults to false here. Enabling it is a deliberate,
audited action taken through the admin UI — never a deploy-time default (§4.2).
"""

from .base import *  # noqa: F401,F403
from .base import run_boot_guards

ENVIRONMENT = "production"

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

run_boot_guards(ENVIRONMENT)
