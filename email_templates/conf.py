"""Settings for the templates component, read from settings.EMAIL_TEMPLATES.

Defaults follow BUILD.md §10.5 and the open questions in §10.12.
"""

from django.conf import settings

DEFAULTS = {
    # Dotted path to the project's audit writer; None logs instead (BUILD.md §10.5.5).
    "AUDIT_FUNC": None,
    # Variables filled by the platform, never by merge data. unsubscribe_url comes
    # from the unsubscribe component (BUILD.md §10.8.4).
    "SYSTEM_VARIABLES": ["unsubscribe_url"],
    # Must appear in both HTML and text of marketing templates (Q-TPL-1).
    "REQUIRED_MARKETING_VARIABLES": ["unsubscribe_url"],
    # "Oversized message" limits for preflight (Q-TPL-2).
    "MAX_HTML_BYTES": 102_400,
    "MAX_TEXT_BYTES": 102_400,
    # Spec §10.2: avoid URL shorteners of unknown reputation. Preflight warns.
    "URL_SHORTENER_HOSTS": [
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
        "tiny.cc",
    ],
}


def get(name):
    return getattr(settings, "EMAIL_TEMPLATES", {}).get(name, DEFAULTS[name])
