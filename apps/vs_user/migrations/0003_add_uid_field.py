from django.db import migrations, models


def add_uid_if_missing(apps, schema_editor):
    """Add uid column to vs_users_user only on environments where it is absent.

    uid was defined in 0001_initial, but installs that ran migrations before
    it was added have the column missing. This migration patches those
    environments without breaking fresh installs (idempotent — skips if
    the column already exists).
    """
    conn = schema_editor.connection
    if conn.vendor == "mysql":
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'vs_users_user'
                  AND COLUMN_NAME  = 'uid'
            """)
            if cursor.fetchone()[0] > 0:
                return
            cursor.execute(
                "ALTER TABLE vs_users_user ADD COLUMN uid INT UNSIGNED NULL"
            )
    else:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_catalog = current_database()
                  AND table_name    = 'vs_users_user'
                  AND column_name   = 'uid'
            """)
            if cursor.fetchone()[0] > 0:
                return
            cursor.execute(
                "ALTER TABLE vs_users_user ADD COLUMN uid INTEGER NULL"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0002_add_pending_approval_status"),
    ]

    operations = [
        migrations.RunPython(add_uid_if_missing, reverse_code=migrations.RunPython.noop),
    ]
