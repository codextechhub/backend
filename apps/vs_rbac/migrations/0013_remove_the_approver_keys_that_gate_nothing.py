"""Delete the ten approver permissions, because approval is not a permission.

A permission key answers "may this person do this thing", and every one of these
answered nothing at all. Whether somebody may approve a refund, a payout or a
purchase is decided by the workflow stage, which names a role, a group, a
dynamic rule or an organogram position and never consults a permission. These
keys looked exactly like the control and were not it:

    Adaeze wants her deputy Ngozi approving refunds at Brightfield. She opens
    the roles screen, ticks "Approve refunds", saves, and tells Ngozi she is
    set up. Ngozi clicks Approve and is refused, because the stage was asking
    whether her role key is ``finance-adjustment-approver``. Nothing Adaeze
    could see said so.

They were previously withheld from the school-facing picker, which stopped the
screen lying to a school and left it lying to CodeX - the console reads the same
registry without that filter. Hiding a key is a worse answer than not having
one: it leaves the misleading thing in the system and makes its absence a
property of who is looking.

Deleting the ``Permission`` rows cascades their grants away, which is the point
rather than a cost - thirty role grants and ten prebuilt-library grants that
conferred nothing. No capability is lost, because none was ever conferred.

If approval should one day be gated by permission as well as by stage, the key
comes back through the seeder alongside the code that reads it, which is the
order that keeps the two honest.
"""
from django.db import migrations

DEAD_KEYS = [
    "finance.journal.approve",
    "finance.journal.approve_high_value",
    "finance.refund.approve",
    "finance.refund.approve_high_value",
    "finance.writeoff.approve",
    "finance.writeoff.approve_high_value",
    "payments.payout_batch.approve",
    "payments.payout_batch.approve_high_value",
    "procurement.approval.approve",
    "procurement.approval.approve_senior",
]


def remove(apps, schema_editor):
    apps.get_model("vs_rbac", "Permission").objects.filter(
        key__in=DEAD_KEYS,
    ).delete()


def restore(apps, schema_editor):
    """Deliberately not reversible.

    Recreating the rows would restore the keys without their grants, which is
    neither the old state nor a useful one. The seeders are where a permission
    comes into existence; if these are ever wanted again they are re-declared
    there, beside the code that reads them.
    """


class Migration(migrations.Migration):
    dependencies = [("vs_rbac", "0012_school_roles_reach_the_workflow_module")]
    operations = [migrations.RunPython(remove, restore)]
