"""
Production. Real IPs, real recipients, real reputation.

GLOBAL_SEND_ENABLED still defaults to false here. Enabling it is a deliberate,
audited action taken through the admin UI — never a deploy-time default (§4.2).
"""

from .base import *  # noqa: F401,F403
from .base import PRODUCTION_STORAGES, run_boot_guards

ENVIRONMENT = "production"

STORAGES = PRODUCTION_STORAGES
SECURE_SSL_REDIRECT = True
# TLS ends at the reverse proxy (nginx / load balancer), which MUST set
# X-Forwarded-Proto and strip any client-supplied value. Without this every
# request looks like http and is redirected forever.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Probes and Prometheus talk to the container directly over the private network.
SECURE_REDIRECT_EXEMPT = [r"^health/", r"^metrics$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

run_boot_guards(ENVIRONMENT)
