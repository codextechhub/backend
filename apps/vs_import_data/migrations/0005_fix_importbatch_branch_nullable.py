from django.db import migrations


def make_branch_nullable(apps, schema_editor):
    conn = schema_editor.connection

    if conn.vendor != "mysql":
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "ALTER COLUMN branch_id DROP NOT NULL;"
        )
        return

    with conn.cursor() as cursor:
        # Idempotency guard — nothing to do if already nullable.
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

        # Two FK constraints can share the same backing index on branch_id
        # (one from Django's initial ORM migration, one from our RunSQL in 0004).
        # Dropping just one FK still leaves the index locked by the other, so
        # MODIFY COLUMN fails with error 1553. The simplest solution that works
        # across all MariaDB/MySQL versions: disable FK checks for the duration
        # of the column change. The constraints themselves are not touched —
        # they remain fully intact after re-enabling.
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            cursor.execute(
                "ALTER TABLE vs_import_data_importbatch "
                "MODIFY COLUMN branch_id BIGINT NULL"
            )
        finally:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def reverse_branch_nullable(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor == "mysql":
        with conn.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                cursor.execute(
                    "ALTER TABLE vs_import_data_importbatch "
                    "MODIFY COLUMN branch_id BIGINT NOT NULL"
                )
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    else:
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importbatch "
            "ALTER COLUMN branch_id SET NOT NULL;"
        )


class Migration(migrations.Migration):
    """Drop NOT NULL constraint on branch_id in ImportBatch.

    The table has two FK constraints sharing the same backing index on branch_id
    (one from Django's ORM initial migration, one from the RunSQL in 0004).
    Dropping FKs one at a time still leaves the index locked by the survivor,
    causing MODIFY COLUMN to fail with error 1553 on any MariaDB/MySQL version.

    Fix: SET FOREIGN_KEY_CHECKS = 0 for the duration of MODIFY COLUMN. This
    lets MySQL change the column definition without touching the FK index. The
    constraints remain fully intact and are re-enforced when checks are re-enabled.

    Fully idempotent — skips silently if branch_id is already nullable.
    PostgreSQL uses the standard ALTER COLUMN ... DROP NOT NULL syntax.
    """

    dependencies = [
        ("vs_import_data", "0004_importbatch_school_branch"),
    ]

    operations = [
        migrations.RunPython(make_branch_nullable, reverse_code=reverse_branch_nullable),
    ]
