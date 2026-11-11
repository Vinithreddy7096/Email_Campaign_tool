"""§10.7.1: job history is append-only (transitions, attempts, accepted
messages); jobs themselves are never deleted. Enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("delivery", "0003_recipient_jobs")]

    operations = [
        forbid("delivery_job_transition", "UPDATE", "DELETE"),
        forbid("delivery_send_attempt", "UPDATE", "DELETE"),
        forbid("delivery_message", "UPDATE", "DELETE"),
        forbid("delivery_recipient_job", "DELETE"),
    ]
