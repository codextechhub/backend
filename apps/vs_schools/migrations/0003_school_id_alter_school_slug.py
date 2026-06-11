# B23 — flip School from slug-as-primary-key to a surrogate BigAuto id.
#
# State side: Django derives every FK column from the target PK, so a single
# state change here (slug → unique field, id → BigAuto PK) updates the state
# of all 21 referencing FKs across 10 apps automatically.
#
# Database side: one vendor-aware routine that
#   1. drops every FK constraint referencing vs_schools_school,
#   2. drops CHECK constraints that mention the referencing columns
#      (e.g. vs_user's ck_vision_staff_no_school),
#   3. adds + populates School.id,
#   4. rewrites every referencing column from slug values to id values,
#   5. swaps the primary key (slug keeps a UNIQUE constraint),
#   6. converts referencing columns to BIGINT and re-adds plain FK
#      constraints (Django performs on_delete in Python; its DDL emits
#      plain FOREIGN KEY, so this matches what Django would create).
#
# Supports MySQL/MariaDB (local) and PostgreSQL (staging). Irreversible.

import django.core.validators
from django.db import migrations, models


def _refs(apps, school_table):
    """All (db_table, column, null) triples for FKs pointing at School."""
    School = apps.get_model("vs_schools", "School")
    out = []
    for model in apps.get_models():
        for f in model._meta.get_fields():
            if getattr(f, "related_model", None) is School and f.concrete and f.model is model:
                out.append((model._meta.db_table, f.column, f.null))
    # de-dup (proxy/inheritance safety)
    return sorted(set(out))


def _flip_school_pk(apps, schema_editor):
    conn = schema_editor.connection
    vendor = conn.vendor
    if vendor not in ("mysql", "postgresql"):
        raise NotImplementedError(
            f"School PK flip is implemented for MySQL/MariaDB and PostgreSQL, not {vendor!r}."
        )

    School = apps.get_model("vs_schools", "School")
    school = School._meta.db_table
    refs = _refs(apps, school)
    ref_tables = {t for t, _, _ in refs}

    q = (lambda n: f"`{n}`") if vendor == "mysql" else (lambda n: f'"{n}"')

    with conn.cursor() as cur:
        # ------------------------------------------------------------------
        # 1. Discover + drop FK constraints referencing the school table.
        # ------------------------------------------------------------------
        if vendor == "mysql":
            cur.execute(
                """
                SELECT table_name, constraint_name
                FROM information_schema.key_column_usage
                WHERE referenced_table_name = %s AND table_schema = DATABASE()
                """,
                [school],
            )
            fk_constraints = cur.fetchall()
        else:
            cur.execute(
                """
                SELECT c.conrelid::regclass::text, c.conname
                FROM pg_constraint c
                WHERE c.confrelid = %s::regclass AND c.contype = 'f'
                """,
                [school],
            )
            fk_constraints = cur.fetchall()

        for table, name in fk_constraints:
            if vendor == "mysql":
                cur.execute(f"ALTER TABLE {q(table)} DROP FOREIGN KEY {q(name)}")
            else:
                cur.execute(f"ALTER TABLE {q(table)} DROP CONSTRAINT {q(name)}")

        # ------------------------------------------------------------------
        # 2. Discover + drop CHECK constraints that mention a referencing
        #    column on a referencing table (re-added verbatim at the end).
        # ------------------------------------------------------------------
        ref_cols_by_table = {}
        for t, c, _ in refs:
            ref_cols_by_table.setdefault(t, set()).add(c)

        saved_checks = []  # (table, name, clause)
        if vendor == "mysql":
            cur.execute(
                """
                SELECT cc.table_name, cc.constraint_name, cc.check_clause
                FROM information_schema.check_constraints cc
                WHERE cc.constraint_schema = DATABASE()
                """
            )
            rows = cur.fetchall()
        else:
            cur.execute(
                """
                SELECT c.conrelid::regclass::text, c.conname,
                       pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                WHERE c.contype = 'c'
                """
            )
            rows = cur.fetchall()
            # pg_get_constraintdef returns "CHECK (<clause>)" — keep the clause.
            rows = [(t, n, d[6:].strip() if d.upper().startswith("CHECK ") else d) for t, n, d in rows]

        for table, name, clause in rows:
            cols = ref_cols_by_table.get(table, set())
            if any(col in clause for col in cols):
                saved_checks.append((table, name, clause))
                cur.execute(f"ALTER TABLE {q(table)} DROP CONSTRAINT {q(name)}")

        # ------------------------------------------------------------------
        # 3. Add + populate the surrogate id on school.
        # ------------------------------------------------------------------
        cur.execute(f"ALTER TABLE {q(school)} ADD COLUMN {q('id')} BIGINT NULL")
        cur.execute(f"SELECT {q('slug')} FROM {q(school)} ORDER BY created_at, slug")
        slugs = [r[0] for r in cur.fetchall()]
        for n, slug_val in enumerate(slugs, start=1):
            cur.execute(
                f"UPDATE {q(school)} SET {q('id')} = %s WHERE {q('slug')} = %s",
                [n, slug_val],
            )

        # ------------------------------------------------------------------
        # 4. Rewrite every referencing column slug -> id (values become
        #    numeric strings inside the existing varchar columns).
        # ------------------------------------------------------------------
        for table, col, _null in refs:
            if vendor == "mysql":
                cur.execute(
                    f"UPDATE {q(table)} t JOIN {q(school)} s ON t.{q(col)} = s.{q('slug')} "
                    f"SET t.{q(col)} = s.{q('id')}"
                )
            else:
                cur.execute(
                    f"UPDATE {q(table)} t SET {q(col)} = s.{q('id')}::varchar "
                    f"FROM {q(school)} s WHERE t.{q(col)} = s.{q('slug')}"
                )

        # ------------------------------------------------------------------
        # 5. Swap the primary key; slug keeps a UNIQUE constraint.
        # ------------------------------------------------------------------
        if vendor == "mysql":
            cur.execute(f"ALTER TABLE {q(school)} MODIFY {q('id')} BIGINT NOT NULL")
            cur.execute(
                f"ALTER TABLE {q(school)} DROP PRIMARY KEY, ADD PRIMARY KEY ({q('id')}), "
                f"ADD CONSTRAINT {q(school + '_slug_uniq')} UNIQUE ({q('slug')})"
            )
            cur.execute(f"ALTER TABLE {q(school)} MODIFY {q('id')} BIGINT NOT NULL AUTO_INCREMENT")
        else:
            cur.execute(f"ALTER TABLE {q(school)} ALTER COLUMN {q('id')} SET NOT NULL")
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'p'",
                [school],
            )
            pk_name = cur.fetchone()[0]
            cur.execute(f"ALTER TABLE {q(school)} DROP CONSTRAINT {q(pk_name)}")
            cur.execute(f"ALTER TABLE {q(school)} ADD PRIMARY KEY ({q('id')})")
            cur.execute(
                f"ALTER TABLE {q(school)} ADD CONSTRAINT {q(school + '_slug_uniq')} UNIQUE ({q('slug')})"
            )
            seq = f"{school}_id_seq"
            cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {q(seq)} OWNED BY {q(school)}.{q('id')}")
            cur.execute(
                f"ALTER TABLE {q(school)} ALTER COLUMN {q('id')} SET DEFAULT nextval('{seq}')"
            )
            cur.execute(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {q(school)}), 0) + 1, false)")

        # ------------------------------------------------------------------
        # 6. Convert referencing columns to BIGINT and re-add FK constraints.
        # ------------------------------------------------------------------
        for table, col, null in refs:
            nullable = "NULL" if null else "NOT NULL"
            if vendor == "mysql":
                cur.execute(f"ALTER TABLE {q(table)} MODIFY {q(col)} BIGINT {nullable}")
            else:
                cur.execute(
                    f"ALTER TABLE {q(table)} ALTER COLUMN {q(col)} TYPE bigint "
                    f"USING {q(col)}::bigint"
                )
            fk_name = f"fk_{table}_{col}_school"[:60]
            cur.execute(
                f"ALTER TABLE {q(table)} ADD CONSTRAINT {q(fk_name)} "
                f"FOREIGN KEY ({q(col)}) REFERENCES {q(school)} ({q('id')})"
            )

        # ------------------------------------------------------------------
        # 7. Restore the CHECK constraints saved in step 2.
        # ------------------------------------------------------------------
        for table, name, clause in saved_checks:
            clause_sql = clause if clause.strip().startswith("(") else f"({clause})"
            cur.execute(f"ALTER TABLE {q(table)} ADD CONSTRAINT {q(name)} CHECK {clause_sql}")


class Migration(migrations.Migration):
    # Must run after every migration that creates a school FK, so a fresh
    # install replays in the right order.
    dependencies = [
        ("vs_schools", "0002_alter_branch_options_alter_branch_managers"),
        ("vs_admin_console", "0002_initial"),
        ("vs_audit", "0003_alter_compliancerule_options_and_more"),
        ("vs_config", "0003_alter_configurationchangelog_options_and_more"),
        ("vs_finance", "0019_account_ifrs_line"),
        ("vs_import_data", "0008_alter_importbatch_options_alter_importbatch_managers"),
        ("vs_notifications", "0003_alter_notification_options_and_more"),
        ("vs_rbac", "0006_rbacauditlog"),
        ("vs_user", "0013_alter_authattempt_options_alter_autheventlog_options_and_more"),
        ("vs_workflow", "0007_alter_approvaldelegation_options_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="school",
                    name="id",
                    field=models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                migrations.AlterField(
                    model_name="school",
                    name="slug",
                    field=models.SlugField(
                        help_text="URL-safe unique school identifier. Lowercase, hyphen-separated.",
                        max_length=80,
                        unique=True,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="Slug must be lowercase letters/numbers separated by single hyphens.",
                                regex="^[a-z0-9]+(?:-[a-z0-9]+)*$",
                            )
                        ],
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(_flip_school_pk, migrations.RunPython.noop),
            ],
        ),
    ]
