from django.apps import AppConfig


class UiConfig(AppConfig):
    """HTMX views, partials, base templates."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "ui"
