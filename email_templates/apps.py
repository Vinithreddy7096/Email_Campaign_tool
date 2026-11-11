from django.apps import AppConfig


class EmailTemplatesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    # This project imports apps as top-level packages; the repo root is on
    # PYTHONPATH (Dockerfile, pytest, manage.py). BUILD.md §10.5.5 step 1.
    name = "email_templates"
    label = "email_templates"
    verbose_name = "Email templates"
