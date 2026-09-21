from django.apps import AppConfig


class ApiConfig(AppConfig):
    """DRF v1 routers, serializers, token auth (§9 of the spec)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "api"
