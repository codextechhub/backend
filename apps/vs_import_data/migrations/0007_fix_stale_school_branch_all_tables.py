from django.db import migrations


def _drop_column_mysql(cursor, table, column):
    """Drop a column on MySQL/MariaDB, first removing any FK constraints on it."""
    cursor.execute("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, [table, column])
    if cursor.fetchone()[0] == 0:
        return

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


TABLES = [
    "vs_import_data_importvalidationissue",
    "vs_import_data_importjobrowresult",
    "vs_import_data_importrollbackrecord",
    "vs_import_data_importnotification",
    "vs_import_data_importtemplate",
    "vs_import_data_importtemplatecolumn",
]


def remove_stale_school_branch(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        for table in TABLES:
            schema_editor.execute(
                f"ALTER TABLE {table} DROP COLUMN IF EXISTS school_id;"
            )
            schema_editor.execute(
                f"ALTER TABLE {table} DROP COLUMN IF EXISTS branch_id;"
            )
        return

    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            _drop_column_mysql(cursor, table, "school_id")
            _drop_column_mysql(cursor, table, "branch_id")


class Migration(migrations.Migration):
    """Drop stale school_id/branch_id columns from every vs_import_data table
    that has no school/branch FK in the current model."""

    dependencies = [
        ("vs_import_data", "0006_fix_importjob_remove_school_branch"),
    ]

    operations = [
        migrations.RunPython(remove_stale_school_branch,
                             reverse_code=migrations.RunPython.noop),
    ]
