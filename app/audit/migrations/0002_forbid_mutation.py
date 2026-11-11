"""§6.10: the audit log is INSERT+SELECT only, enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [forbid("audit_event", "UPDATE", "DELETE")]
