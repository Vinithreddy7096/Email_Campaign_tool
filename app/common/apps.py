from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Shared utilities, mixins, validators. No Django models."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"
