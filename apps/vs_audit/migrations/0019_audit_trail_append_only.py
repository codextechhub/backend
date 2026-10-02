"""Make the platform audit trail append-only at the database.

``AuditEvent`` refused ``save()`` and ``delete()`` on an instance, but a
queryset ``update()`` or ``delete()`` never calls either, so bulk paths (a
user deletion's SET_NULL, a tidy-up command, a data migration) went straight
through. BEFORE UPDATE and BEFORE DELETE triggers now refuse every write but
two:

* filling a blank ``branch_id`` with every other column unchanged, which is
  how ``manage.py branch_backfill`` places an old finance or procurement
  event (the same narrowing ``vs_finance`` 0042 gives its own trail);
* deleting the events of the one tenant named in the transaction-local
  setting ``vs_audit.discard_tenant``, which only
  :func:`vs_audit.services.discard_trail_of_unused_tenant` sets, inside the
  transaction that deletes a tenant nobody ever used.

The user columns become PROTECT: a SET_NULL would be an UPDATE the trigger
refuses, so a person in the trail is deactivated rather than deleted, and the
refusal is a clear ProtectedError rather than a database exception.

PostgreSQL only, which is what every environment runs; on another vendor the
Python guard on the model remains the protection. Reversible: the reverse
drops the triggers and the function.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TABLE = "vs_audit_auditevent"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_audit_auditevent_block() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.branch_id IS NULL
       AND NEW.branch_id IS NOT NULL
       AND (to_jsonb(NEW) - 'branch_id') = (to_jsonb(OLD) - 'branch_id') THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE'
       AND OLD.tenant_id IS NOT NULL
       AND current_setting('vs_audit.discard_tenant', true) = OLD.tenant_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'AuditEvent rows are append-only and cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_audit_auditevent_no_update ON {TABLE};
CREATE TRIGGER vs_audit_auditevent_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_audit_auditevent_block();

DROP TRIGGER IF EXISTS vs_audit_auditevent_no_delete ON {TABLE};
CREATE TRIGGER vs_audit_auditevent_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_audit_auditevent_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_audit_auditevent_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_audit_auditevent_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_audit_auditevent_block();
"""


def install_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)


def drop_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_audit", "0018_audit_event_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="actor_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="performed_audit_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="effective_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="effective_audit_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(install_triggers, drop_triggers),
    ]
