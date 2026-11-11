from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Shared utilities, mixins, validators. No Django models."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self) -> None:
        from django.core.checks import register

        from common.grants import deploy_check

        # deploy=True: only `check --deploy` connects to the database for this.
        register(deploy_check, deploy=True)
