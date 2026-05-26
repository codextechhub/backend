from django.db import migrations


class Migration(migrations.Migration):
    """Drop school_id and branch_id from ImportJob on the cloud DB.

    The pre-squash migration set added these FK columns to ImportJob, but the
    current model has no such fields. Django omits them from INSERT statements,
    causing a NOT NULL constraint violation when creating a job.
    """

    dependencies = [
        ("vs_import_data", "0005_fix_importbatch_branch_nullable"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE vs_import_data_importjob
                    DROP COLUMN IF EXISTS branch_id;

                ALTER TABLE vs_import_data_importjob
                    DROP COLUMN IF EXISTS school_id;
            """,
            reverse_sql="",
        ),
    ]
