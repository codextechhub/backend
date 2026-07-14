"""Enforce FinanceAuditLog append-only at the DB level.

The model blocks ``save()``/``delete()`` in Python, but a queryset ``.update()`` /
``.delete()`` bypasses those hooks and writes straight to the table. This migration
installs BEFORE UPDATE and BEFORE DELETE triggers that raise, so the append-only
guarantee holds even for bulk/ORM-bypassing writes.

Vendor-branched: PostgreSQL (trigger function + RAISE EXCEPTION) is what the platform
actually runs (local/CI/staging); a MySQL/MariaDB branch (SIGNAL SQLSTATE '45000') is
kept for the legacy fallback. Fully reversible — the reverse drops the triggers and
(Postgres) the function.
"""
from django.db import migrations

TABLE = "vs_finance_financeauditlog"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_finance_financeauditlog_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'FinanceAuditLog rows are immutable and cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_update ON {TABLE};
CREATE TRIGGER vs_finance_financeauditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_finance_financeauditlog_block();

DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_delete ON {TABLE};
CREATE TRIGGER vs_finance_financeauditlog_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_finance_financeauditlog_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_finance_financeauditlog_block();
"""

MYSQL_FORWARD = f"""
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_update;
CREATE TRIGGER vs_finance_financeauditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW
    SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'FinanceAuditLog rows are immutable and cannot be updated or deleted.';
"""

MYSQL_FORWARD_DELETE = f"""
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_delete;
CREATE TRIGGER vs_finance_financeauditlog_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW
    SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'FinanceAuditLog rows are immutable and cannot be updated or deleted.';
"""

MYSQL_REVERSE = """
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_update;
DROP TRIGGER IF EXISTS vs_finance_financeauditlog_no_delete;
"""


def install_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)
    elif vendor == "mysql":
        # MariaDB/MySQL only allow one statement per execute for CREATE TRIGGER.
        schema_editor.execute(MYSQL_FORWARD)
        schema_editor.execute(MYSQL_FORWARD_DELETE)
    # Other vendors (e.g. sqlite in a throwaway context): the Python-level model
    # guard remains the protection; no DB trigger is installed.


def drop_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)
    elif vendor == "mysql":
        schema_editor.execute(MYSQL_REVERSE)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(install_triggers, drop_triggers),
    ]
