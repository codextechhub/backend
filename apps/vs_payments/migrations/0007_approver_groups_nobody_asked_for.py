"""Remove the payout approver groups a tenant never asked for, and the steps naming them.

Provisioning a tenant's books used to publish a checker and a high-value ladder and
mint the groups its steps named. A tenant that had said nothing about who signs off on
money leaving ended up holding two groups it never created, with nobody in them, and a
high-value threshold that was a guess at its scale rather than its own answer.

Who approves a tenant's cash-out is read from the organogram that tenant builds, so the
books now arrive holding a payout route and nothing inside it. This clears what the
earlier behaviour left behind: the empty groups, and the steps that named them. The
route itself stays, empty, which is the intended end state. It keeps the tenant off the
shared platform row, and a batch submitted against it is refused as unconfigured rather
than paid unseen.

Three rules keep this from touching anything anybody chose:

* a group with members is somebody's decision and is left alone, whatever its code;
* a step a batch actually ran through is evidence, so a group whose steps are not all
  deletable is left whole rather than half-cleared;
* only the codes this app seeded are in scope, so a group a tenant built itself, or one
  another app seeded, is never considered.

The reverse is a no-op. The rows carried no decision, so there is nothing to restore,
and provisioning would not recreate them: not publishing them is the point.
"""
from django.db import migrations


# The group codes this app seeded, written out rather than imported from
# ``vs_payments.constants``. A migration has to keep meaning what it meant on the day it
# was written, and a constant renamed later would silently change which rows this
# deletes.
SEEDED_GROUP_CODES = (
    "payout-approver",
    "payout-senior-approver",
)


def remove_approver_groups_nobody_asked_for(apps, schema_editor):
    """Delete each unused seeded group and its steps, skipping anything in use.

    Order matters: ``WorkflowStage.approver_group`` is PROTECT, so the steps go first
    and the group follows. ``WorkflowStageInstance.stage`` and
    ``WorkflowInstance.current_stage`` are PROTECT too, which is the database refusing
    to erase a step a batch ran through; both are checked up front so an in-use group is
    skipped whole rather than failing the deploy part-way.
    """
    WorkflowApproverGroup = apps.get_model("vs_workflow", "WorkflowApproverGroup")
    WorkflowApproverGroupMember = apps.get_model(
        "vs_workflow", "WorkflowApproverGroupMember")
    WorkflowInstance = apps.get_model("vs_workflow", "WorkflowInstance")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowStageInstance = apps.get_model("vs_workflow", "WorkflowStageInstance")

    for group in WorkflowApproverGroup.objects.filter(code__in=SEEDED_GROUP_CODES):
        # A staffed group is somebody's decision, whatever its code says.
        if WorkflowApproverGroupMember.objects.filter(group=group).exists():
            continue
        stage_ids = list(
            WorkflowStage.objects
            .filter(approver_group=group)
            .values_list("pk", flat=True)
        )
        # A step that has run, or that something is waiting on, stays as evidence.
        if WorkflowStageInstance.objects.filter(stage_id__in=stage_ids).exists():
            continue
        if WorkflowInstance.objects.filter(current_stage_id__in=stage_ids).exists():
            continue
        WorkflowStage.objects.filter(pk__in=stage_ids).delete()
        group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_payments", "0006_seed_platform_payout_approval_fallback"),
        ("vs_workflow", "0008_retarget_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.RunPython(
            remove_approver_groups_nobody_asked_for,
            migrations.RunPython.noop,
        ),
    ]
