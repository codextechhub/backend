"""Give every notification to the tenant that reads it, not the one it is about.

``Notification.tenant`` was doing two jobs: it named the tenant the message was
ABOUT and it decided which tenant could READ the row. For most events those are
the same party, so the ambiguity never showed. Support tickets are the first
flow where they differ: ``vs_tickets`` dispatches to platform triage staff and
passed ``tenant=ticket.tenant``, so every row addressed to a Codex agent was
stamped with the school.

Two things followed from that, both live in shipped data:

  * The agent could not see the in-app row. Their feed goes through
    ``TenantAwareManager``, which filters on their own tenant.
  * A school administrator holding ``communication.message_activity.audit``
    could read those rows in the history log, including the rendered body of an
    INTERNAL ticket note, because the log filters on the same column.

So ownership moves to the recipient's own tenant and the subject is recorded
separately in ``origin_tenant``. Shipped rows have to be rewritten, which is the
point: the internal notes already sitting in schools' history logs are the
reason the fix cannot be code-only.

**The rewrite is 0011, not this migration, and the split is required rather than
tidy.** Django defers a new column's index to the end of the migration's
transaction. With the ``RunPython`` in here, that ordering became:

    ADD COLUMN origin_tenant  ->  UPDATE every row  ->  CREATE INDEX

and Postgres refuses the third step outright - ``cannot CREATE INDEX ... because
it has pending trigger events`` - because the UPDATE queued deferred foreign-key
trigger events against the same table. The migration failed on every database
that had a single notification row in it.

Splitting keeps both halves atomic on their own, which is what actually matters
here: the data rewrite is still all-or-nothing, so a school's history log cannot
be left half-corrected. Making this migration non-atomic would have bought the
same ordering at the cost of that guarantee.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0009_unquote_background_task_copy"),
        ("vs_tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="origin_tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                help_text=(
                    "The tenant the message is ABOUT, when the caller named one. "
                    "For a school's support ticket notified to platform staff this "
                    "is the school while tenant is codex. Internal-only: never "
                    "serialized, and never a filter a school-tenant caller can "
                    "reach, or it becomes a second route to another tenant's rows."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="originated_notifications",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="tenant",
            field=models.ForeignKey(
                help_text=(
                    "OWNER of the record: whose inbox it appears in and whose "
                    "history log it belongs to. Always the recipient's own tenant. "
                    "What the message is ABOUT lives in origin_tenant."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="notifications",
                to="vs_tenants.tenant",
            ),
        ),
    ]
