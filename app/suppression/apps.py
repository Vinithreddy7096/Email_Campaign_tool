from django.apps import AppConfig


class SuppressionConfig(AppConfig):
    """Global and scoped suppression. Rows are never deleted (§6.5)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "suppression"
