from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    """The operations console: the main operator UI (docs/design/console-mockup.html)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "console"
