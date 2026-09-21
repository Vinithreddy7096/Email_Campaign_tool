"""
Shared settings. Every value is env-driven (BUILD.md §4).

Concrete environments (local/staging/production) import * from here and then call
``run_boot_guards(ENVIRONMENT)`` as their final statement. The guards implement
BUILD.md §4.2.

Note on enforcement: §4.2 is written with ``assert``. Bare asserts are stripped
when Python runs under ``-O``, which would silently disable the interlock that
keeps MOCK_MODE off in production. These are implemented as explicit raises of
ImproperlyConfigured so they cannot be optimised away.
"""

from __future__ import annotations

import os
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# app/config/settings/base.py -> app/
BASE_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
# Read .env from the repo root if present. In containers the values normally
# arrive as real environment variables and this is a no-op.
_dotenv = REPO_ROOT / ".env"
if _dotenv.exists():
    env.read_env(str(_dotenv))

# ─── Core ────────────────────────────────────────────────────────────────
SECRET_KEY = env.str("SECRET_KEY", default="")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
BASE_URL = env.str("BASE_URL", default="http://localhost:8000")

# ─── Safety (§4.2, §5.3) ─────────────────────────────────────────────────
MOCK_MODE = env.bool("MOCK_MODE", default=True)
# Defaults to false even in production. Turning it on is a deliberate, audited
# human action performed through the admin UI, not a deploy.
GLOBAL_SEND_ENABLED = env.bool("GLOBAL_SEND_ENABLED", default=False)
ALLOWED_TEST_RECIPIENT_DOMAINS = env.list("ALLOWED_TEST_RECIPIENT_DOMAINS", default=[])

# ─── Database ────────────────────────────────────────────────────────────
DB_CONN_MAX_AGE = env.int("DB_CONN_MAX_AGE", default=60)
DB_STATEMENT_TIMEOUT_MS = env.int("DB_STATEMENT_TIMEOUT_MS", default=30000)
DATABASES = {
    "default": {
        **env.db_url("DATABASE_URL", default="postgres://mailer:mailer@postgres:5432/mailer"),
        "CONN_MAX_AGE": DB_CONN_MAX_AGE,
        "OPTIONS": {"options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── Broker / cache ──────────────────────────────────────────────────────
RABBITMQ_URL = env.str("RABBITMQ_URL", default="amqp://mailer:mailer@rabbitmq:5672//")
REDIS_URL = env.str("REDIS_URL", default="redis://redis:6379/0")

# config/celery.py loads settings with the CELERY_ namespace, so this is what
# actually points Celery at RabbitMQ. Without it Celery silently falls back to
# amqp://guest@localhost and every worker retries a refused connection forever
# while still reporting itself as "running".
CELERY_BROKER_URL = RABBITMQ_URL
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# ─── MTA handoff ─────────────────────────────────────────────────────────
# In MOCK_MODE these point at the MailHog sink and the delivery service MUST
# ignore any per-domain routing held in the database (§5.3 item 1).
MTA_SMTP_HOST = env.str("MTA_SMTP_HOST", default="mailhog")
MTA_SMTP_PORT = env.int("MTA_SMTP_PORT", default=1025)
MTA_SMTP_USE_TLS = env.bool("MTA_SMTP_USE_TLS", default=False)
MTA_SMTP_USERNAME = env.str("MTA_SMTP_USERNAME", default="")
MTA_SMTP_PASSWORD = env.str("MTA_SMTP_PASSWORD", default="")
MTA_SUBMISSION_TIMEOUT_S = env.int("MTA_SUBMISSION_TIMEOUT_S", default=30)
MTA_MAX_CONNECTIONS = env.int("MTA_MAX_CONNECTIONS", default=20)

# ─── Identity / DNS ──────────────────────────────────────────────────────
DEFAULT_RETURN_PATH_DOMAIN = env.str("DEFAULT_RETURN_PATH_DOMAIN", default="")
DEFAULT_TRACKING_DOMAIN = env.str("DEFAULT_TRACKING_DOMAIN", default="")
MESSAGE_ID_DOMAIN = env.str("MESSAGE_ID_DOMAIN", default="")
DKIM_SELECTOR_DEFAULT = env.str("DKIM_SELECTOR_DEFAULT", default="s1")
DKIM_KEY_DIR = env.str("DKIM_KEY_DIR", default="/etc/opendkim/keys")

# ─── Rate governance (§9.3) ──────────────────────────────────────────────
GLOBAL_MAX_MSGS_PER_MINUTE = env.int("GLOBAL_MAX_MSGS_PER_MINUTE", default=200)
DEFAULT_PROVIDER_MAX_CONCURRENCY = env.int("DEFAULT_PROVIDER_MAX_CONCURRENCY", default=5)
DEFAULT_PROVIDER_MAX_PER_HOUR = env.int("DEFAULT_PROVIDER_MAX_PER_HOUR", default=2000)
CAMPAIGN_EXPANSION_BATCH_SIZE = env.int("CAMPAIGN_EXPANSION_BATCH_SIZE", default=500)

# ─── Retry policy (§9.4) ─────────────────────────────────────────────────
RETRY_BACKOFF_SECONDS = [
    int(s)
    for s in env.list(
        "RETRY_BACKOFF_SECONDS", default=["300", "900", "3600", "10800", "21600", "43200"]
    )
]
MAX_DELIVERY_ATTEMPTS = env.int("MAX_DELIVERY_ATTEMPTS", default=6)
MESSAGE_MAX_AGE_HOURS = env.int("MESSAGE_MAX_AGE_HOURS", default=72)

# ─── Suppression (§8.2) ──────────────────────────────────────────────────
SOFT_BOUNCE_SUPPRESS_THRESHOLD = env.int("SOFT_BOUNCE_SUPPRESS_THRESHOLD", default=5)
SOFT_BOUNCE_WINDOW_DAYS = env.int("SOFT_BOUNCE_WINDOW_DAYS", default=30)
COMPLAINT_RATE_ALERT_THRESHOLD = env.float("COMPLAINT_RATE_ALERT_THRESHOLD", default=0.001)
HARD_BOUNCE_RATE_ALERT_THRESHOLD = env.float("HARD_BOUNCE_RATE_ALERT_THRESHOLD", default=0.02)

# ─── Consent (§8.3) ──────────────────────────────────────────────────────
# Read as a string first: the variable ships empty, and an empty value would
# blow up an int cast. None = no age ceiling on marketing consent.
_consent_max_age = env.str("CONSENT_MAX_AGE_DAYS", default="").strip()
CONSENT_MAX_AGE_DAYS = int(_consent_max_age) if _consent_max_age else None

# ─── Tracking (privacy defaults: OFF) ────────────────────────────────────
TRACKING_OPENS_ENABLED = env.bool("TRACKING_OPENS_ENABLED", default=False)
TRACKING_CLICKS_ENABLED = env.bool("TRACKING_CLICKS_ENABLED", default=False)
TRACKING_RETENTION_DAYS = env.int("TRACKING_RETENTION_DAYS", default=90)

# ─── Storage ─────────────────────────────────────────────────────────────
S3_ENDPOINT_URL = env.str("S3_ENDPOINT_URL", default="")
S3_BUCKET = env.str("S3_BUCKET", default="")
S3_ACCESS_KEY = env.str("S3_ACCESS_KEY", default="")
S3_SECRET_KEY = env.str("S3_SECRET_KEY", default="")
S3_REGION = env.str("S3_REGION", default="us-east-1")

# ─── Observability ───────────────────────────────────────────────────────
PROMETHEUS_METRICS_ENABLED = env.bool("PROMETHEUS_METRICS_ENABLED", default=True)
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_FORMAT = env.str("LOG_FORMAT", default="json")
SENTRY_DSN = env.str("SENTRY_DSN", default="")

# ─── Security ────────────────────────────────────────────────────────────
MFA_REQUIRED_ROLES = env.list("MFA_REQUIRED_ROLES", default=["SYSTEM_ADMIN", "APPROVER"])
API_TOKEN_DEFAULT_TTL_DAYS = env.int("API_TOKEN_DEFAULT_TTL_DAYS", default=90)
WEBHOOK_SIGNING_SECRET = env.str("WEBHOOK_SIGNING_SECRET", default="")
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=28800)
ABUSE_CONTACT_EMAIL = env.str("ABUSE_CONTACT_EMAIL", default="")

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the token
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# ─── Django plumbing ─────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "django_htmx",
    # Platform apps. NB: the templating app is deliberately NOT called
    # "templates" — that name collides with Django template discovery (§3).
    "accounts",
    "domains",
    "contacts",
    "consent",
    "suppression",
    "templating",
    "campaigns",
    "delivery",
    "events",
    "reporting",
    "audit",
    "api",
    "ui",
    "common",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "ui" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Renders the [MOCK] banner on every page (§5.3 item 5).
                "common.context_processors.environment_banner",
            ],
        },
    }
]

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if LOG_FORMAT == "json" else "plain",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}


# ─── §4.2 boot guards ────────────────────────────────────────────────────
def run_boot_guards(environment: str) -> None:
    """Refuse to boot on an unsafe configuration.

    Called as the last statement of each concrete settings module. Raises
    ImproperlyConfigured — never a bare assert — so ``python -O`` cannot
    disable it.
    """
    problems: list[str] = []

    if environment == "production":
        if MOCK_MODE is not False:
            problems.append("MOCK_MODE must be false in production")
        if DEBUG is not False:
            problems.append("DEBUG must be false in production")
        if not SECRET_KEY or len(SECRET_KEY) < 50:
            problems.append("SECRET_KEY must be set and at least 50 characters")
        if not BASE_URL.startswith("https://"):
            problems.append("BASE_URL must be https:// in production")
        if not MESSAGE_ID_DOMAIN:
            problems.append("MESSAGE_ID_DOMAIN is required — Message-IDs must use a domain we own")
        if not DEFAULT_RETURN_PATH_DOMAIN:
            problems.append("DEFAULT_RETURN_PATH_DOMAIN is required for VERP bounce routing")
        if not ABUSE_CONTACT_EMAIL:
            problems.append(
                "ABUSE_CONTACT_EMAIL is required — a published abuse contact is mandatory"
            )
        if not WEBHOOK_SIGNING_SECRET:
            problems.append("WEBHOOK_SIGNING_SECRET is required to authenticate provider webhooks")
        problems.extend(_check_dkim_key_dir())

    else:
        # Non-prod may not send to arbitrary recipients.
        if MOCK_MODE is not True and not ALLOWED_TEST_RECIPIENT_DOMAINS:
            problems.append(
                "Non-production requires MOCK_MODE=true or a non-empty "
                "ALLOWED_TEST_RECIPIENT_DOMAINS allowlist"
            )

    if problems:
        raise ImproperlyConfigured(
            f"Refusing to boot in '{environment}':\n  - " + "\n  - ".join(problems)
        )


def _check_dkim_key_dir() -> list[str]:
    """DKIM private keys must exist and must not be world-readable."""
    path = Path(DKIM_KEY_DIR)
    if not path.is_dir():
        return [f"DKIM_KEY_DIR does not exist or is not a directory: {DKIM_KEY_DIR}"]
    if os.name != "posix":
        # Mode bits are not meaningful on Windows; production is Linux.
        return []
    mode = path.stat().st_mode
    if mode & 0o007:
        return [f"DKIM_KEY_DIR is world-accessible (mode {mode & 0o777:o}): {DKIM_KEY_DIR}"]
    return []
