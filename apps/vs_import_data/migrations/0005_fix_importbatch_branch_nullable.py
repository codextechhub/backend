from django.db import migrations


class Migration(migrations.Migration):
    """Drop NOT NULL constraint on branch_id in ImportBatch.

    On the cloud DB, branch_id was provisioned as NOT NULL before the
    0004 migration ran. That migration used ADD COLUMN IF NOT EXISTS, so
    the column already existing meant the NOT NULL constraint was never
    dropped. This migration fixes it explicitly.
    """

    dependencies = [
        ("vs_import_data", "0004_importbatch_school_branch"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE vs_import_data_importbatch ALTER COLUMN branch_id DROP NOT NULL;",
            reverse_sql="ALTER TABLE vs_import_data_importbatch ALTER COLUMN branch_id SET NOT NULL;",
        ),
    ]
