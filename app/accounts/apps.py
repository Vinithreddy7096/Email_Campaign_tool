from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Users, roles, MFA, API tokens (BUILD.md §6.1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
