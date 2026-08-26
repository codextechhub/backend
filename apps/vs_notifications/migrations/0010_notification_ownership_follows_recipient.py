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

So this migration moves ownership to the recipient's own tenant and records the
subject separately in ``origin_tenant``. It rewrites shipped rows, which is the
point: the internal notes already sitting in schools' history logs are the
reason the fix cannot be code-only.

Reversible: the reverse restores each row's original owner from origin_tenant
before the column is dropped.
"""
from django.db import migrations, models
import django.db.models.deletion


def move_ownership_to_recipient(apps, schema_editor):
    Notification = apps.get_model("vs_notifications", "Notification")

    # Every existing row was stamped with the tenant it is about, so that value
    # is exactly what origin_tenant is meant to hold.
    Notification.objects.all().update(origin_tenant=models.F("tenant"))

    # Hand the mis-owned rows to their recipient. Grouped by the recipient's
    # tenant because a queryset update cannot assign from a joined column;
    # in practice this is one pass per tenant that has ever received a record
    # about somebody else, which today means the platform tenant alone.
    mis_owned = (
        Notification.objects
        .filter(recipient__isnull=False)
        .exclude(recipient__tenant=models.F("tenant"))
    )
    owner_ids = list(
        mis_owned.values_list("recipient__tenant_id", flat=True).distinct()
    )
    for owner_id in owner_ids:
        Notification.objects.filter(recipient__tenant_id=owner_id).exclude(
            tenant_id=owner_id
        ).update(tenant_id=owner_id)


def restore_original_ownership(apps, schema_editor):
    Notification = apps.get_model("vs_notifications", "Notification")
    Notification.objects.filter(origin_tenant__isnull=False).update(
        tenant=models.F("origin_tenant")
    )


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
        migrations.RunPython(
            move_ownership_to_recipient,
            restore_original_ownership,
        ),
    ]
