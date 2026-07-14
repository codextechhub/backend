from django.db import migrations


TABLE = "vs_config_configurationauditevent"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_config_audit_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ConfigurationAuditEvent rows are immutable.';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS vs_config_audit_no_update ON {TABLE};
CREATE TRIGGER vs_config_audit_no_update BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_config_audit_block();
DROP TRIGGER IF EXISTS vs_config_audit_no_delete ON {TABLE};
CREATE TRIGGER vs_config_audit_no_delete BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_config_audit_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_config_audit_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_config_audit_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_config_audit_block();
"""

MYSQL_UPDATE = f"""
CREATE TRIGGER vs_config_audit_no_update BEFORE UPDATE ON {TABLE}
FOR EACH ROW SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'ConfigurationAuditEvent rows are immutable.';
"""
MYSQL_DELETE = f"""
CREATE TRIGGER vs_config_audit_no_delete BEFORE DELETE ON {TABLE}
FOR EACH ROW SIGNAL SQLSTATE '45000'
SET MESSAGE_TEXT = 'ConfigurationAuditEvent rows are immutable.';
"""


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)
    elif vendor == "mysql":
        schema_editor.execute("DROP TRIGGER IF EXISTS vs_config_audit_no_update")
        schema_editor.execute("DROP TRIGGER IF EXISTS vs_config_audit_no_delete")
        schema_editor.execute(MYSQL_UPDATE)
        schema_editor.execute(MYSQL_DELETE)


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)
    elif vendor == "mysql":
        schema_editor.execute("DROP TRIGGER IF EXISTS vs_config_audit_no_update")
        schema_editor.execute("DROP TRIGGER IF EXISTS vs_config_audit_no_delete")


class Migration(migrations.Migration):
    dependencies = [("vs_config", "0002_initial")]
    operations = [migrations.RunPython(install, uninstall)]
