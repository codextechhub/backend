from django.db import migrations


class Migration(migrations.Migration):
    """Drop stale school_id/branch_id columns from every vs_import_data table
    that has no school/branch FK in the current model.

    The pre-squash migration set added these columns (often as NOT NULL) to
    most import tables. The current squashed model removed them from all tables
    except ImportBatch. Any table that still carries them will raise an
    IntegrityError on INSERT because Django omits unknown fields.

    All statements use DROP COLUMN IF EXISTS so this migration is fully
    idempotent: environments that never had these columns are unaffected.
    """

    dependencies = [
        ("vs_import_data", "0006_fix_importjob_remove_school_branch"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                -- ImportValidationIssue
                ALTER TABLE vs_import_data_importvalidationissue
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importvalidationissue
                    DROP COLUMN IF EXISTS branch_id;

                -- ImportJobRowResult
                ALTER TABLE vs_import_data_importjobrowresult
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importjobrowresult
                    DROP COLUMN IF EXISTS branch_id;

                -- ImportRollbackRecord
                ALTER TABLE vs_import_data_importrollbackrecord
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importrollbackrecord
                    DROP COLUMN IF EXISTS branch_id;

                -- ImportNotification
                ALTER TABLE vs_import_data_importnotification
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importnotification
                    DROP COLUMN IF EXISTS branch_id;

                -- ImportTemplate
                ALTER TABLE vs_import_data_importtemplate
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importtemplate
                    DROP COLUMN IF EXISTS branch_id;

                -- ImportTemplateColumn
                ALTER TABLE vs_import_data_importtemplatecolumn
                    DROP COLUMN IF EXISTS school_id;
                ALTER TABLE vs_import_data_importtemplatecolumn
                    DROP COLUMN IF EXISTS branch_id;
            """,
            reverse_sql="",
        ),
    ]
