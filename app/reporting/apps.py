from django.apps import AppConfig


class ReportingConfig(AppConfig):
    """Aggregates, dashboards, exports."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reporting"
