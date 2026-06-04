from django.db import migrations


def _drop_column_mysql(cursor, table, column):
    """Drop a column on MySQL/MariaDB, first removing any FK constraints on it.

    DROP COLUMN implicitly tries to DROP the column's backing index. If that
    index backs a FK constraint, MySQL raises error 1553. The fix: find and
    drop all FK constraints referencing the column before dropping it.
    """
    cursor.execute("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, [table, column])
    if cursor.fetchone()[0] == 0:
        return  # column doesn't exist — nothing to do

    cursor.execute("""
        SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, [table, column])
    for (fk_name,) in cursor.fetchall():
        cursor.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk_name}`")

    cursor.execute(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")


def remove_importjob_school_branch(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importjob DROP COLUMN IF EXISTS branch_id;"
        )
        schema_editor.execute(
            "ALTER TABLE vs_import_data_importjob DROP COLUMN IF EXISTS school_id;"
        )
        return

    with schema_editor.connection.cursor() as cursor:
        _drop_column_mysql(cursor, "vs_import_data_importjob", "branch_id")
        _drop_column_mysql(cursor, "vs_import_data_importjob", "school_id")


class Migration(migrations.Migration):
    """Drop school_id and branch_id from ImportJob on the cloud DB."""

    dependencies = [
        ("vs_import_data", "0005_fix_importbatch_branch_nullable"),
    ]

    operations = [
        migrations.RunPython(remove_importjob_school_branch,
                             reverse_code=migrations.RunPython.noop),
    ]
