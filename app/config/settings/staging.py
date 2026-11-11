"""
Staging. This is the first environment with a real Postfix host (BUILD.md §5.1).

Staging still must not send to arbitrary public recipients: either MOCK_MODE
stays true or ALLOWED_TEST_RECIPIENT_DOMAINS is populated. The boot guard
enforces that.
"""

from .base import *  # noqa: F401,F403
from .base import PRODUCTION_STORAGES, run_boot_guards

ENVIRONMENT = "staging"

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
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

run_boot_guards(ENVIRONMENT)
