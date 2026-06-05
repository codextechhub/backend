from django.db import migrations


CONSTRAINTS = [
    (
        "ck_vision_staff_no_school",
        "(user_type = 'CX_STAFF' AND school_id IS NULL AND branch_id IS NULL)"
        " OR user_type != 'CX_STAFF'",
    ),
    (
        "ck_school_bound_users",
        "user_type = 'CX_STAFF' OR school_id IS NOT NULL",
    ),
    (
        "ck_branch_required_for_branch_level_users",
        "user_type IN ('CX_STAFF', 'SCHOOL_ADMIN') OR branch_id IS NOT NULL",
    ),
]


def fix_constraints(apps, schema_editor):
    """Drop and recreate the three user check constraints with the correct SQL.

    Environments that ran 0001_initial from an older codebase may have a more
    restrictive definition (e.g. ck_school_bound_users without the CX_STAFF
    exception), which causes IntegrityError when creating platform users.
    Uses INFORMATION_SCHEMA to check existence before dropping so this is
    fully idempotent on all environments.
    """
    conn = schema_editor.connection

    if conn.vendor == "mysql":
        with conn.cursor() as cursor:
            for name, check_sql in CONSTRAINTS:
                cursor.execute("""
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'vs_users_user'
                      AND CONSTRAINT_NAME = %s
                      AND CONSTRAINT_TYPE = 'CHECK'
                """, [name])
                if cursor.fetchone()[0] > 0:
                    cursor.execute(
                        f"ALTER TABLE vs_users_user DROP CONSTRAINT `{name}`"
                    )
                cursor.execute(
                    f"ALTER TABLE vs_users_user "
                    f"ADD CONSTRAINT `{name}` CHECK ({check_sql})"
                )
    else:
        for name, check_sql in CONSTRAINTS:
            schema_editor.execute(
                f"ALTER TABLE vs_users_user "
                f"DROP CONSTRAINT IF EXISTS {name}"
            )
            schema_editor.execute(
                f"ALTER TABLE vs_users_user "
                f"ADD CONSTRAINT {name} CHECK ({check_sql})"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0003_add_uid_field"),
    ]

    operations = [
        migrations.RunPython(fix_constraints, reverse_code=migrations.RunPython.noop),
    ]
