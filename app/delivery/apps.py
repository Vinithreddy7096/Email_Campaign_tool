from django.apps import AppConfig


class DeliveryConfig(AppConfig):
    """Recipient jobs, messages, rate governance (§6.8)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "delivery"
