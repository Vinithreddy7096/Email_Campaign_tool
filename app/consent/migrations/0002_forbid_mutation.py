"""§6.4: consent records are append-only, enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("consent", "0001_initial")]

    operations = [forbid("consent_record", "UPDATE", "DELETE")]
