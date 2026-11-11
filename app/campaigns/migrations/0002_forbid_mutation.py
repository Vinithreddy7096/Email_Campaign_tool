"""§10.6.1: approval snapshots are immutable, enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("campaigns", "0001_initial")]

    operations = [
        forbid("campaigns_snapshot", "UPDATE", "DELETE"),
        forbid("campaigns_snapshot_recipient", "UPDATE", "DELETE"),
        forbid("campaigns_campaign", "DELETE"),
    ]
