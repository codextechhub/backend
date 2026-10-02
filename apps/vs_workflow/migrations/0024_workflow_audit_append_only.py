"""Make the workflow audit log, the record of who approved what, append-only.

BEFORE UPDATE and BEFORE DELETE triggers refuse every write to
``WorkflowAuditLog``; the model refuses the same in Python. Its foreign keys
are PROTECT, so no deletion elsewhere ever needs to change a row.

PostgreSQL only, which is what every environment runs. Reversible: the reverse
drops the triggers and the function.
"""

from django.db import migrations

TABLE = "vs_workflow_workflowauditlog"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_workflow_workflowauditlog_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'WorkflowAuditLog rows are append-only and cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_workflow_workflowauditlog_no_update ON {TABLE};
CREATE TRIGGER vs_workflow_workflowauditlog_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_workflow_workflowauditlog_block();

DROP TRIGGER IF EXISTS vs_workflow_workflowauditlog_no_delete ON {TABLE};
CREATE TRIGGER vs_workflow_workflowauditlog_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_workflow_workflowauditlog_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_workflow_workflowauditlog_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_workflow_workflowauditlog_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_workflow_workflowauditlog_block();
"""


def install_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)


def drop_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0023_existing_delegations_say_who_set_them_up"),
    ]

    operations = [
        migrations.RunPython(install_triggers, drop_triggers),
    ]
