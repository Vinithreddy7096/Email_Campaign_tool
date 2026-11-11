"""§10.9: rate limits and warm-up plans are switched off, never deleted, so the
audit log can always say which limits held a message. Enforced in the database."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("delivery", "0006_rate_limits_warmup")]

    operations = [
        forbid("delivery_rate_limit", "DELETE"),
        forbid("delivery_warmup_plan", "DELETE"),
    ]
