"""Route already-published procurement approval stages by the document's own branch.

``vs_procurement.approvals`` now publishes both seeded stages with
``approver_scope="BRANCH"``, but the seeder only runs from the demo seed command, the
approval-setup endpoint and the new per-tenant seeding command - never on deploy.
Environments provisioned before this change therefore keep ``PLATFORM`` stages, and
``PLATFORM`` forwards no branch to RBAC at all: an approver whose authority is granted
at one site is never resolved for that site's own requests (everything there parks),
while a tenant-wide grant is handed every site's requests indiscriminately. This
migration closes that on existing rows.

``BRANCH`` forwards the document's branch, so eligibility becomes "holders assigned at
this document's branch, plus holders assigned tenant-wide". A document with no branch
forwards ``None`` and resolves to tenant-wide holders only - which is byte-identical to
the ``PLATFORM`` behaviour being replaced, so a tenant with no branches is unaffected.

Scope: strictly the four procurement document types, and strictly stages still set to
``PLATFORM``. Finance approval templates and payout-batch templates share the same
``WorkflowStage`` table and are deliberately left alone; a stage an administrator has
already moved to ``SCHOOL`` or ``BRANCH`` is left alone too, since it is their decision,
not this migration's.

Reverse: restores ``PLATFORM`` on the same rows. It is a true inverse, so rolling back
re-blocks branch-assigned approvers. Idempotent in both directions: a filtered ``UPDATE``
on one column, so re-running changes nothing.
"""
from django.db import migrations

# Copied literals, not an import from constants: a data migration must keep describing
# the world as it was when it ran, even if the token set changes later.
PROCUREMENT_DOCUMENT_TYPES = (
    "procurement.requisition",
    "procurement.purchase_order",
    "procurement.vendor_invoice",
    "procurement.vendor_payment",
)


def _reroute(apps, from_scope, to_scope):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowStage.objects.filter(
        template__document_type__in=PROCUREMENT_DOCUMENT_TYPES,
        approver_scope=from_scope,
    ).update(approver_scope=to_scope)


def route_by_branch(apps, schema_editor):
    """Move platform-scoped procurement stages onto the document's own branch."""
    _reroute(apps, "PLATFORM", "BRANCH")


def route_platform_wide(apps, schema_editor):
    """Reverse: send procurement stages back to platform scope. See module docstring."""
    _reroute(apps, "BRANCH", "PLATFORM")


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0024_approvaloverride"),
        ("vs_workflow", "0002_seed_platform_user_creation_template"),
    ]

    operations = [
        migrations.RunPython(route_by_branch, route_platform_wide),
    ]
