from django.apps import AppConfig


class ConsentConfig(AppConfig):
    """Append-only consent records and evidence (§6.4)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "consent"
