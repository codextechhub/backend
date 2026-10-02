"""Make the payments event log append-only at the database.

``PaymentEvent`` refused ``save()`` and ``delete()`` on an instance only, so a
queryset write (a user deletion's SET_NULL, a tidy-up command) went through.
BEFORE UPDATE and BEFORE DELETE triggers refuse every write, and the user
columns become PROTECT so a person in the log is deactivated, never deleted,
with a clear ProtectedError rather than a database exception.

PostgreSQL only, which is what every environment runs; elsewhere the Python
guard on the model remains the protection. Reversible: the reverse drops the
triggers and the function.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TABLE = "vs_payments_paymentevent"

PG_FORWARD = f"""
CREATE OR REPLACE FUNCTION vs_payments_paymentevent_block() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'PaymentEvent rows are append-only and cannot be updated or deleted.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS vs_payments_paymentevent_no_update ON {TABLE};
CREATE TRIGGER vs_payments_paymentevent_no_update
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_payments_paymentevent_block();

DROP TRIGGER IF EXISTS vs_payments_paymentevent_no_delete ON {TABLE};
CREATE TRIGGER vs_payments_paymentevent_no_delete
    BEFORE DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION vs_payments_paymentevent_block();
"""

PG_REVERSE = f"""
DROP TRIGGER IF EXISTS vs_payments_paymentevent_no_update ON {TABLE};
DROP TRIGGER IF EXISTS vs_payments_paymentevent_no_delete ON {TABLE};
DROP FUNCTION IF EXISTS vs_payments_paymentevent_block();
"""


def install_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_FORWARD)


def drop_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PG_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0011_held_disputes_and_reconciliation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="paymentevent",
            name="actor_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="paymentevent",
            name="proxied_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(install_triggers, drop_triggers),
    ]
