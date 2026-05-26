from django.db import migrations


class Migration(migrations.Migration):
    """Add school_id and branch_id to ImportBatch if they don't exist.

    These columns are defined in 0001_initial but were absent from the
    production DB because it was provisioned from a pre-squash migration
    set that did not include them. All statements use IF NOT EXISTS so
    this migration is safe on environments that already have the columns.
    """

    dependencies = [
        ("vs_import_data", "0003_importtemplate_version"),
        ("vs_schools", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE vs_import_data_importbatch
                    ADD COLUMN IF NOT EXISTS school_id BIGINT
                        REFERENCES vs_schools_school(id) ON DELETE CASCADE;

                ALTER TABLE vs_import_data_importbatch
                    ADD COLUMN IF NOT EXISTS branch_id BIGINT
                        REFERENCES vs_schools_branch(id) ON DELETE CASCADE;

                CREATE INDEX IF NOT EXISTS vs_import_d_school__f81fa8_idx
                    ON vs_import_data_importbatch (school_id, status);

                CREATE INDEX IF NOT EXISTS vs_import_d_school__c45910_idx
                    ON vs_import_data_importbatch (school_id, dataset_type);

                CREATE INDEX IF NOT EXISTS vs_import_d_branch__e52e55_idx
                    ON vs_import_data_importbatch (branch_id, status);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS vs_import_d_school__f81fa8_idx;
                DROP INDEX IF EXISTS vs_import_d_school__c45910_idx;
                DROP INDEX IF EXISTS vs_import_d_branch__e52e55_idx;

                ALTER TABLE vs_import_data_importbatch
                    DROP COLUMN IF EXISTS school_id;

                ALTER TABLE vs_import_data_importbatch
                    DROP COLUMN IF EXISTS branch_id;
            """,
        ),
    ]
