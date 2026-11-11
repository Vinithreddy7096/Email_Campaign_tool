"""§6.5: suppression rows are never deleted, enforced in the database.

UPDATE stays allowed so STREAM/LIST rows can be lifted and erased addresses
blanked; a CHECK constraint keeps GLOBAL rows from ever being lifted.
"""

from django.db import migrations

from common.db import forbid


class Migration(migrations.Migration):
    dependencies = [("suppression", "0001_initial")]

    operations = [forbid("suppression", "DELETE")]
