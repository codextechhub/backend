"""Give each finance audit entry the branch of the document it is about.

The column is nullable: an entry about something that belongs to the whole
tenant has no branch, and entries written before this column existed are given
theirs later by ``manage.py branch_backfill``. That command has to write the
column on rows the immutability triggers from ``0003`` refuse to change, so the
update trigger is narrowed to let exactly one change through: a blank branch
filled in, with every other column as it was. Moving a branch, clearing one, or
filling one while changing anything else is still refused, and deletes are
refused as before.

Fully reversible: the reverse puts the ``0003`` trigger back before the column
goes, so a table rewound past this migration is exactly as it was.
"""

import django.db.models.deletion
from django.db import migrations, models

TABLE = "vs_finance_financeauditlog"
MESSAGE = "FinanceAuditLog rows are immutable and cannot be updated or deleted."

PG_FILL_BRANCH_ONLY = f"""
CREATE OR REPLACE FUNCTION vs_finance_financeauditlog_block() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.branch_id IS NULL
       AND NEW.branch_id IS NOT NULL
       AND (to_jsonb(NEW) - 'branch_id') = (to_jsonb(OLD) - 'branch_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '{MESSAGE}';
END;
$$ LANGUAGE plpgsql;
"""

PG_BLOCK_ALL = f"""
CREATE OR REPLACE FUNCTION vs_finance_financeauditlog_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{MESSAGE}';
END;
$$ LANGUAGE plpgsql;
"""

#: Every column but ``branch_id``, which is the one a fill may change.
_UNCHANGED = " AND ".join(
    f"NEW.`{column}` <=> OLD.`{column}`" for column in (
        "id", "entity_id", "actor_id", "effective_user_id", "action", "status",
        "target_type", "target_id", "document_number", "message", "before", "after",
        "metadata", "created_at",
    )
)

MYSQL_FILL_BRANCH_ONLY = f"""
CREATE TRIGGER vs_finance_financeauditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW
    BEGIN
        IF NOT (OLD.branch_id IS NULL AND NEW.branch_id IS NOT NULL AND {_UNCHANGED}) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{MESSAGE}';
        END IF;
    END
"""

MYSQL_BLOCK_ALL = f"""
CREATE TRIGGER vs_finance_financeauditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW
    SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = '{MESSAGE}';
"""

MYSQL_DROP = "DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_update;"


def allow_branch_fill(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_FILL_BRANCH_ONLY)
    elif vendor == "mysql":
        schema_editor.execute(MYSQL_DROP)
        schema_editor.execute(MYSQL_FILL_BRANCH_ONLY)


def block_every_update(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_BLOCK_ALL)
    elif vendor == "mysql":
        schema_editor.execute(MYSQL_DROP)
        schema_editor.execute(MYSQL_BLOCK_ALL)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0041_ar_guards_data"),
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.AddField(
            model_name="financeauditlog",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch of the document the entry is about; null for the whole tenant.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="finance_audit_logs",
                to="vs_tenants.branch",
            ),
        ),
        migrations.AddIndex(
            model_name="financeauditlog",
            index=models.Index(
                fields=["entity", "branch"], name="vs_finance_audit_branch_idx"
            ),
        ),
        migrations.RunPython(allow_branch_fill, block_every_update),
    ]
