from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    """Campaigns, approvals, preflight, scheduling (§6.7)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "campaigns"
