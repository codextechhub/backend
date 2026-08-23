"""Stop already-published procurement approval stages from approving spend themselves.

``vs_procurement.approvals.ensure_default_approval_templates`` now publishes both
seeded stages with ``skip_if_no_approvers=False``, but it only runs from the demo seed
command and the approval-setup endpoint - never on deploy. Environments provisioned
before this change therefore keep stages that auto-skip when nobody holds the approving
permission, and a document submitted there reaches a terminal APPROVED decision with no
human involved. This migration closes that on existing rows.

Scope: strictly the four procurement document types. Finance approval templates and
payout-batch templates share the same ``WorkflowStage`` table and are deliberately left
alone; their skip policy is their own module's decision.

Reverse: restores ``skip_if_no_approvers=True`` on the same rows. It is a true inverse,
so rolling this migration back **re-opens the self-approval hole** and procurement spend
can again approve itself when no approver holds the permission. That is the price of a
reversible migration and is stated here so the choice is visible at rollback time.

Idempotent in both directions: it is a filtered ``UPDATE`` on a boolean column, so
re-running changes nothing.
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


def _set_skip(apps, value):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowStage.objects.filter(
        template__document_type__in=PROCUREMENT_DOCUMENT_TYPES,
    ).exclude(skip_if_no_approvers=value).update(skip_if_no_approvers=value)


def block_when_unstaffed(apps, schema_editor):
    """Make every published procurement stage park instead of auto-skipping."""
    _set_skip(apps, False)


def restore_auto_skip(apps, schema_editor):
    """Reverse: re-enable auto-skip, which re-opens self-approval. See module docstring."""
    _set_skip(apps, True)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0022_purchaseordervendordelivery_and_more"),
        ("vs_workflow", "0002_seed_platform_user_creation_template"),
    ]

    operations = [
        migrations.RunPython(block_when_unstaffed, restore_auto_skip),
    ]
