"""What CodeX seeds is a starting point, not a cage.

Two things a school could not change about its own setup, and both were
preloading mistaken for ownership.

**The baseline roles were locked.** School Admin, Branch Admin and Teacher were
seeded ``is_locked=True``, so the roles screen showed a school its own most
important roles read-only. A school wanting its admin to configure a payment
gateway had to create a second role carrying that one permission and assign it
alongside - a workaround for a rule that should not have been there. Which
permissions a school may hold at all is already bounded by
``PermissionScope.TENANT`` and enforced on the grant models; locking the role on
top of that decided, on the school's behalf, that CodeX's first guess was final.

**The seeded ladders held steps nobody chose.** Each ladder named its approver
through a role key CodeX invented - ``payout-approver`` and its five siblings -
which existed only so a ROLE stage could resolve, and which no school ever asked
for. Those stages go, and the roles with them.

What is left is a template with no steps, which is now a safe state rather than
a silent one. ``submit_for_approval`` and the finance direct-post gate both
refuse a stageless template with ``ApprovalNotConfiguredError``, and both take a
confirmation recorded against the person who gives it. So a school either
configures its steps or posts deliberately, and either way somebody decided -
where before the ladder decided by naming a role nobody held.

Not reversible. Restoring a stage means choosing its approver, and choosing is
exactly what this hands back to the school.
"""
from django.db import migrations

APPROVER_ROLE_KEYS = [
    "finance-adjustment-approver",
    "finance-senior-adjustment-approver",
    "payout-approver",
    "payout-senior-approver",
    "procurement-approver",
    "procurement-senior-approver",
]


def hand_it_over(apps, schema_editor):
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")
    Stage = apps.get_model("vs_workflow", "WorkflowStage")

    # A school may shape every role it holds, including the ones it started with.
    Role.objects.filter(tenant__kind="SCHOOL", is_locked=True).update(is_locked=False)

    Instance = apps.get_model("vs_workflow", "WorkflowInstance")
    StageInstance = apps.get_model("vs_workflow", "WorkflowStageInstance")
    Assignment = apps.get_model("vs_rbac", "TenantUserRoleAssignment")

    roles = Role.objects.filter(key__in=APPROVER_ROLE_KEYS, tenant__kind="SCHOOL")
    # Both the anchor and the key are matched, because a stage can carry one
    # without the other.
    stages = Stage.objects.filter(approver_role__in=roles) | Stage.objects.filter(
        approver_role_key__in=APPROVER_ROLE_KEYS
    )
    stages = stages.distinct()

    # A stage a real document has run through is not configuration any more, it
    # is evidence: it records how that document came to be approved and by
    # which step. ``WorkflowStageInstance.stage`` and
    # ``WorkflowInstance.current_stage`` are PROTECT for exactly that reason,
    # and forcing past them would delete the answer to "how was this approved"
    # for documents already posted. So history stays, and the roles those
    # stages anchor stay with it - a handful, against the many that no document
    # ever touched.
    used = set(
        StageInstance.objects.filter(stage__in=stages).values_list("stage_id", flat=True)
    ) | set(
        Instance.objects.filter(current_stage__in=stages)
        .values_list("current_stage_id", flat=True)
    )
    Stage.objects.filter(pk__in=stages.exclude(pk__in=used).values("pk")).delete()

    # Whatever is left anchored is anchored by that history. PROTECT decides
    # which roles survive, rather than a list here that would go stale.
    still_anchored = set(
        Stage.objects.filter(approver_role__in=roles)
        .values_list("approver_role_id", flat=True)
    )
    # And a role somebody actually holds is somebody's access, whatever CodeX
    # meant by creating it. Removing it here would take that person's approval
    # authority away with nothing on any screen saying so, which is a decision
    # for the school that assigned it. ``TenantUserRoleAssignment.role`` is
    # PROTECT for that reason and is left to make the call.
    held = set(
        Assignment.objects.filter(role__in=roles).values_list("role_id", flat=True)
    )
    roles.exclude(pk__in=still_anchored | held).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vs_rbac", "0013_remove_the_approver_keys_that_gate_nothing"),
        ("vs_workflow", "0011_flag_provisioned_approver_roles"),
    ]
    operations = [migrations.RunPython(hand_it_over, migrations.RunPython.noop)]
