"""The role set is fixed by the application, so it is created here rather than
by an operator — every environment has the same roles with the same codes."""

from django.db import migrations

ROLES = {
    "SYSTEM_ADMIN": "Platform configuration, users, domains, kill switch. MFA required.",
    "APPROVER": "Approves campaigns before they can be scheduled. MFA required.",
    "CAMPAIGN_MANAGER": "Builds and schedules campaigns; cannot approve their own.",
    "CONTENT_EDITOR": "Edits templates; cannot send.",
    "ANALYST": "Read-only access to reporting.",
}


def create_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for code, description in ROLES.items():
        Role.objects.update_or_create(code=code, defaults={"description": description})


def remove_roles(apps, schema_editor):
    apps.get_model("accounts", "Role").objects.filter(code__in=ROLES).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_role_apitoken_userrole")]

    operations = [migrations.RunPython(create_roles, remove_roles)]
