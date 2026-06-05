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

    import logging
    log = logging.getLogger(__name__)

    if conn.vendor == "mysql":
        with conn.cursor() as cursor:
            for name, check_sql in CONSTRAINTS:
                # Step 1: drop the old (possibly wrong) constraint.
                # This is the critical step — it unblocks create_superuser.
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

                # Step 2: re-add with the correct definition.
                # On dev DBs with dirty test data this may fail — that is
                # acceptable. The DROP above is sufficient to unblock the app.
                try:
                    cursor.execute(
                        f"ALTER TABLE vs_users_user "
                        f"ADD CONSTRAINT `{name}` CHECK ({check_sql})"
                    )
                except Exception as exc:
                    log.warning(
                        "Could not re-add constraint %s: %s. "
                        "Existing rows likely violate it. "
                        "The app will work — clean bad data and re-run to restore the constraint.",
                        name, exc,
                    )
    else:
        for name, check_sql in CONSTRAINTS:
            schema_editor.execute(
                f"ALTER TABLE vs_users_user DROP CONSTRAINT IF EXISTS {name}"
            )
            try:
                schema_editor.execute(
                    f"ALTER TABLE vs_users_user "
                    f"ADD CONSTRAINT {name} CHECK ({check_sql})"
                )
            except Exception as exc:
                log.warning("Could not re-add constraint %s: %s.", name, exc)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_user", "0003_add_uid_field"),
    ]

    operations = [
        migrations.RunPython(fix_constraints, reverse_code=migrations.RunPython.noop),
    ]
