from django.apps import AppConfig


class TemplatingConfig(AppConfig):
    """Templates and immutable approved versions (§6.6). NOT named 'templates'."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "templating"
