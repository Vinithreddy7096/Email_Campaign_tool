"""§10.9: policy decisions are append-only; pauses are never deleted (resuming
updates the row). Enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("delivery", "0001_initial")]

    operations = [
        forbid("delivery_policy_decision", "UPDATE", "DELETE"),
        forbid("delivery_send_pause", "DELETE"),
    ]
