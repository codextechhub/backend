from django.db import migrations


class Migration(migrations.Migration):
    """Drop the legacy version column that was added directly to production.

    The field was never part of the Django model, so only a database-level
    DROP is needed — no ORM state change required.
    """

    dependencies = [
        ("vs_import_data", "0002_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE vs_import_data_importtemplate DROP COLUMN IF EXISTS version;",
            reverse_sql=(
                "ALTER TABLE vs_import_data_importtemplate "
                "ADD COLUMN version VARCHAR(20) NOT NULL DEFAULT '1.0';"
            ),
        ),
    ]
