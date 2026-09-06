"""Retire the approval steps that survived as evidence, so they stop routing.

``vs_rbac.0014`` removed the approver roles CodeX used to create in every tenant
and the steps that named them. It deliberately kept the steps a real document had
already run through, because such a step is not configuration any more: it records
how that document came to be approved and by which step, and PROTECT exists on
``WorkflowStageInstance.stage`` for exactly that reason.

Keeping the row was right. Leaving it *live* was not, and only shows up against
data with history:

    Lagoon View raises a requisition. Its own ladder lost its steps with the
    roles, so it resolves to the shared platform ladder, which kept its two steps
    because other schools' documents had run through them. The first step asks
    for holders of ``procurement-approver`` - a role that no longer exists in any
    tenant - so nobody is eligible, and the step is configured never to skip
    itself. The requisition parks. Nobody can be appointed to free it, because
    the role it wants cannot be granted, so it parks for good, with a screen that
    says only that it is waiting for an approver.

Retiring is the distinction the model already has. The routing services skip a
retired step (``advance_instance`` records ``stage_retired``), the template
screens list live steps only, and the row and its history stay exactly where they
are. A tenant whose every step is retired then reads as unconfigured and is
refused with ``ApprovalNotConfiguredError``, which names the problem and offers a
recorded way through, instead of parking silently for ever.

Reversible: un-retiring restores the previous state exactly, which is the state
this corrects rather than one worth returning to, but nothing is lost by allowing
it.
"""
from django.db import migrations
from django.utils import timezone

APPROVER_ROLE_KEYS = [
    "finance-adjustment-approver",
    "finance-senior-adjustment-approver",
    "payout-approver",
    "payout-senior-approver",
    "procurement-approver",
    "procurement-senior-approver",
]


def stop_them_routing(apps, schema_editor):
    Stage = apps.get_model("vs_workflow", "WorkflowStage")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")

    # Both the key and the anchor, because a step can carry one without the
    # other, and the anchor may point at a role that survived because somebody
    # holds it.
    roles = Role.objects.filter(key__in=APPROVER_ROLE_KEYS)
    stages = Stage.objects.filter(approver_role_key__in=APPROVER_ROLE_KEYS) | \
        Stage.objects.filter(approver_role__in=roles)

    Stage.objects.filter(
        pk__in=stages.filter(retired_at__isnull=True).values("pk"),
    ).update(retired_at=timezone.now())


def let_them_route_again(apps, schema_editor):
    Stage = apps.get_model("vs_workflow", "WorkflowStage")
    Role = apps.get_model("vs_rbac", "TenantRoleTemplate")

    roles = Role.objects.filter(key__in=APPROVER_ROLE_KEYS)
    stages = Stage.objects.filter(approver_role_key__in=APPROVER_ROLE_KEYS) | \
        Stage.objects.filter(approver_role__in=roles)

    Stage.objects.filter(pk__in=stages.values("pk")).update(retired_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_workflow", "0011_flag_provisioned_approver_roles"),
        ("vs_rbac", "0014_a_school_owns_the_roles_and_rules_codex_set_up"),
    ]
    operations = [migrations.RunPython(stop_them_routing, let_them_route_again)]
