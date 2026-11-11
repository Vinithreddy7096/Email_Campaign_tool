"""§10.8: delivery events and operator reviews are append-only; raw payloads are
never deleted (they may be redacted for an erasure request). Enforced in the DB."""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [
        forbid("events_delivery_event", "UPDATE", "DELETE"),
        forbid("events_review", "UPDATE", "DELETE"),
        forbid("events_raw_payload", "DELETE"),
    ]
