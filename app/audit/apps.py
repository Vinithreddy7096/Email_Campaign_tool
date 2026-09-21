from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Immutable audit log. INSERT+SELECT only (§6.10)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"
