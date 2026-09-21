from django.apps import AppConfig


class DomainsConfig(AppConfig):
    """Sending domains, IPs, pools, sender identities (§6.2)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "domains"
