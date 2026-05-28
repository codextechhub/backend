import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Drop NOT NULL constraint on branch_id in ImportBatch.

    On the cloud DB, branch_id was provisioned as NOT NULL before the
    0004 migration ran. That migration used ADD COLUMN IF NOT EXISTS, so
    the column already existing meant the NOT NULL constraint was never
    dropped. This migration fixes it explicitly.

    Uses AlterField (not RunSQL) so Django generates the correct DDL for
    both MariaDB (local) and PostgreSQL (cloud).
    """

    dependencies = [
        ("vs_import_data", "0004_importbatch_school_branch"),
        ("vs_schools", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="importbatch",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="import_batches",
                to="vs_schools.branch",
            ),
        ),
    ]
