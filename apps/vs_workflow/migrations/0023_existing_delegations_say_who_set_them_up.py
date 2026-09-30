"""Existing delegations say who set them up and when they reached waiting requests.

Each was set up by its own delegator, since nobody else could create one. One
that has already started is marked as applied, because it has been reaching
stages as they opened since it started: applying it now would reach back into
stages that opened before it existed, which is a decision nobody took. One that
starts later is left unapplied, so the sweep carries it to the waiting requests
when it starts, as it does any delegation created from here on.
"""
from django.db import migrations
from django.db.models import F


def describe_existing_delegations(apps, schema_editor):
    from django.utils import timezone

    ApprovalDelegation = apps.get_model("vs_workflow", "ApprovalDelegation")
    ApprovalDelegation.objects.filter(created_by__isnull=True).update(
        created_by=F("delegator"),
    )
    ApprovalDelegation.objects.filter(
        applied_at__isnull=True, starts_at__lte=timezone.now(),
    ).update(applied_at=F("starts_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0022_approvers_change_while_a_request_waits"),
    ]

    operations = [
        migrations.RunPython(describe_existing_delegations, migrations.RunPython.noop),
    ]
