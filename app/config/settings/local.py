"""Local development. MOCK_MODE is expected to be true; all mail goes to MailHog."""

from .base import *  # noqa: F401,F403
from .base import SECRET_KEY, run_boot_guards

ENVIRONMENT = "local"

# Convenience only. The production guard requires a real 50+ char key, so this
# can never leak into a real deployment.
if not SECRET_KEY:
    SECRET_KEY = "insecure-local-only-do-not-use-anywhere-else"  # noqa: S105

INTERNAL_IPS = ["127.0.0.1"]

# No collectstatic step locally: serve CSS straight from the app directories.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

run_boot_guards(ENVIRONMENT)
