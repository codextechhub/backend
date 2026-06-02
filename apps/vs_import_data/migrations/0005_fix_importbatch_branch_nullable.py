from django.db import migrations


def make_branch_nullable(apps, schema_editor):
    conn = schema_editor.connection

    if conn.vendor != "mysql":
        # PostgreSQL and others support the standard syntax directly.
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "ALTER COLUMN branch_id DROP NOT NULL;"
        )
        return

    with conn.cursor() as cursor:
        # Idempotency guard — skip if already nullable.
        cursor.execute("""
            SELECT IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'vs_import_data_importbatch'
              AND COLUMN_NAME  = 'branch_id'
        """)
        row = cursor.fetchone()
        if row and row[0] == "YES":
            return

        # MariaDB/MySQL will not let MODIFY COLUMN touch a column whose index
        # is backing a FK constraint. We must drop the FK first, change the
        # column, then put the FK back.
        cursor.execute("""
            SELECT CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA        = DATABASE()
              AND TABLE_NAME          = 'vs_import_data_importbatch'
              AND COLUMN_NAME         = 'branch_id'
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """)
        fk_names = [r[0] for r in cursor.fetchall()]

        for fk in fk_names:
            cursor.execute(
                f"ALTER TABLE vs_import_data_importbatch DROP FOREIGN KEY `{fk}`"
            )

        cursor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "MODIFY COLUMN branch_id BIGINT NULL"
        )

        cursor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "ADD CONSTRAINT FOREIGN KEY (branch_id) "
            "REFERENCES vs_schools_branch(id) ON DELETE CASCADE"
        )


def reverse_branch_nullable(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor == "mysql":
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "MODIFY COLUMN branch_id BIGINT NOT NULL;"
        )
    else:
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "ALTER COLUMN branch_id SET NOT NULL;"
        )


class Migration(migrations.Migration):
    """Drop NOT NULL constraint on branch_id in ImportBatch.

    MariaDB/MySQL will not allow MODIFY COLUMN on a column whose index backs
    a FK constraint unless the FK is dropped first. This migration:
      1. Queries INFORMATION_SCHEMA to find the FK constraint name dynamically
         (the name is auto-generated and differs per environment)
      2. Drops the FK constraint
      3. Makes the column nullable via MODIFY COLUMN
      4. Re-adds the FK constraint

    PostgreSQL gets the standard ALTER COLUMN ... DROP NOT NULL.
    Fully idempotent — skips silently if the column is already nullable.
    """

    dependencies = [
        ("vs_import_data", "0004_importbatch_school_branch"),
    ]

    operations = [
        migrations.RunPython(make_branch_nullable, reverse_code=reverse_branch_nullable),
    ]
