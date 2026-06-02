from django.db import migrations


def make_branch_nullable(apps, schema_editor):
    db = schema_editor.connection.vendor
    if db == "mysql":
        # MariaDB/MySQL: MODIFY COLUMN changes nullability in-place without
        # touching the FK constraint index — ALTER COLUMN syntax is not supported.
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch MODIFY COLUMN branch_id BIGINT NULL;"
        )
    else:
        # PostgreSQL and others support the standard ALTER COLUMN syntax.
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch ALTER COLUMN branch_id DROP NOT NULL;"
        )


def reverse_branch_nullable(apps, schema_editor):
    db = schema_editor.connection.vendor
    if db == "mysql":
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch MODIFY COLUMN branch_id BIGINT NOT NULL;"
        )
    else:
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch ALTER COLUMN branch_id SET NOT NULL;"
        )


class Migration(migrations.Migration):
    """Drop NOT NULL constraint on branch_id in ImportBatch.

    Uses RunPython with vendor detection so MariaDB/MySQL gets MODIFY COLUMN
    (which leaves the FK constraint index intact) while PostgreSQL gets the
    standard ALTER COLUMN ... DROP NOT NULL syntax.
    """

    dependencies = [
        ("vs_import_data", "0004_importbatch_school_branch"),
    ]

    operations = [
        migrations.RunPython(make_branch_nullable, reverse_code=reverse_branch_nullable),
    ]
