"""Hand every existing notification to the tenant that reads it.

The schema half is 0010. This is the data half, and the two are separate
because Django defers a new column's index to the end of its migration's
transaction: with the rewrite in 0010 the order became ADD COLUMN, UPDATE every
row, CREATE INDEX, and Postgres refuses to index a table that has pending
trigger events queued by those updates. See 0010's docstring.

Atomic on its own, which is the guarantee worth keeping: the rows being
rewritten include INTERNAL ticket notes sitting in schools' history logs, and a
half-applied pass would leave some of them still readable by the wrong tenant.
"""
from django.db import migrations, models


def move_ownership_to_recipient(apps, schema_editor):
    Notification = apps.get_model("vs_notifications", "Notification")

    # Every existing row was stamped with the tenant it is about, so that value
    # is exactly what origin_tenant is meant to hold.
    Notification.objects.all().update(origin_tenant=models.F("tenant"))

    # Hand the mis-owned rows to their recipient. Grouped by the recipient's
    # tenant because a queryset update cannot assign from a joined column; in
    # practice this is one pass per tenant that has ever received a record about
    # somebody else, which today means the platform tenant alone.
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
    """Put each row back under the tenant it is about.

    Runs BEFORE 0010 drops the column, because the reverse of the pair is
    applied newest first - which is the whole reason origin_tenant is written
    for every row above rather than only for the mis-owned ones.
    """
    Notification = apps.get_model("vs_notifications", "Notification")
    Notification.objects.filter(origin_tenant__isnull=False).update(
        tenant=models.F("origin_tenant")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_notifications", "0010_notification_ownership_follows_recipient"),
    ]

    operations = [
        migrations.RunPython(
            move_ownership_to_recipient,
            restore_original_ownership,
        ),
    ]
