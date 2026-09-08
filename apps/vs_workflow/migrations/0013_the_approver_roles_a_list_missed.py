"""Retire the remaining provisioned approver steps, found by asking the data.

``vs_rbac.0014`` removed the approver roles CodeX minted in every tenant, and
``vs_workflow.0012`` retired the steps that named them. Both worked from a
written-down list of six keys, and the list was short by one:
``finance-expense-claim-approver`` was never on it, so five schools still carry
that role with a live step pointing at it, nobody holding it, and no way to
appoint anybody - the exact state those two migrations existed to clear.

So this one does not carry a list. It reads the keys out of the published steps,
the way ``0011`` reads them, which is the only version that cannot go stale: a
key invented tomorrow is covered because the step naming it is what gets found.

**What counts as one of CodeX's, and what does not.** Three conditions together,
and each excludes something real:

* the key ends in ``-approver`` - the shape provisioning used, and no role in the
  prebuilt library is named that way, so School Admin and its four siblings
  cannot be caught;
* the role is ``is_system_role`` with no ``created_by`` - the split
  ``0011`` drew between "the platform made this" and "somebody typed this into
  the roles screen";
* nobody holds it. A role somebody holds is somebody's access, whatever CodeX
  meant by creating it, and taking it away here would remove that person's
  authority with nothing on any screen saying so. ``lagoon-view/payout-approver``
  is exactly that case and is left alone.

Steps are retired rather than deleted, for the reason ``0012`` gives: a step a
real document ran through records how that document was approved. Retired, it
stops routing and stays as evidence, and a tenant left with no live step is
asked to confirm rather than parking for ever.

Not reversible. Un-retiring would restore steps that route to nobody, and
nothing here records which rows it touched.
"""
from django.db import migrations, models
from django.utils import timezone


def ask_the_data(apps, schema_editor):
    Stage = apps.get_model("vs_workflow", "WorkflowStage")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    Assignment = apps.get_model("vs_rbac", "TenantUserRoleAssignment")
    StageInstance = apps.get_model("vs_workflow", "WorkflowStageInstance")
    Instance = apps.get_model("vs_workflow", "WorkflowInstance")

    # Every key a live step actually names, rather than a list of the ones we
    # remembered. `-approver` is provisioning's own shape; see the docstring.
    named = set(
        Stage.objects.filter(retired_at__isnull=True)
        .exclude(approver_role_key="")
        .values_list("approver_role_key", flat=True)
    )
    keys = sorted(k for k in named if k.endswith("-approver"))
    if not keys:
        return

    held_ids = set(
        Assignment.objects.values_list("role_id", flat=True)
    )
    roles = Role.objects.filter(
        key__in=keys, is_system_role=True, created_by__isnull=True,
    ).exclude(pk__in=held_ids)
    if not roles.exists():
        return

    role_keys = set(roles.values_list("key", flat=True))
    role_ids = set(roles.values_list("pk", flat=True))

    # Both the key and the anchor: a step can carry one without the other.
    stages = Stage.objects.filter(retired_at__isnull=True).filter(
        models.Q(approver_role_key__in=role_keys)
        | models.Q(approver_role_id__in=role_ids)
    )
    stage_ids = set(stages.values_list("pk", flat=True))

    # History is what decides between deleting a step and retiring it, and it is
    # the same distinction ``vs_rbac.0014`` drew. A step a real document ran
    # through records how that document was approved and must survive; PROTECT
    # on ``WorkflowStageInstance.stage`` says so. A step nothing ever ran through
    # is configuration nobody chose, and retiring it would leave the role it
    # anchors on the school's roles screen for ever - which is the row the school
    # was asking about in the first place.
    used = set(
        StageInstance.objects.filter(stage_id__in=stage_ids)
        .values_list("stage_id", flat=True)
    ) | set(
        Instance.objects.filter(current_stage_id__in=stage_ids)
        .values_list("current_stage_id", flat=True)
    )
    Stage.objects.filter(pk__in=stage_ids - used).delete()
    Stage.objects.filter(pk__in=used).update(retired_at=timezone.now())

    # Whatever is still anchored is anchored by a step kept as evidence, which
    # PROTECT holds on to; the rest go with their steps.
    still_anchored = set(
        Stage.objects.filter(approver_role_id__in=role_ids)
        .values_list("approver_role_id", flat=True)
    )
    Role.objects.filter(pk__in=role_ids - still_anchored).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0012_a_step_kept_as_evidence_stops_routing"),
        ("vs_rbac", "0014_a_school_owns_the_roles_and_rules_codex_set_up"),
    ]
    operations = [migrations.RunPython(ask_the_data, migrations.RunPython.noop)]
