from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Users, roles, MFA, API tokens (BUILD.md §6.1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self) -> None:
        from django.contrib.auth.signals import user_logged_in

        from accounts.middleware import clear_mfa_on_login

        user_logged_in.connect(clear_mfa_on_login, dispatch_uid="accounts.clear_mfa_on_login")
