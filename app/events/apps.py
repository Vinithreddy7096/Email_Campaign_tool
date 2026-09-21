from django.apps import AppConfig


class EventsConfig(AppConfig):
    """DSN/FBL/webhook ingest and normalization (§6.9)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
