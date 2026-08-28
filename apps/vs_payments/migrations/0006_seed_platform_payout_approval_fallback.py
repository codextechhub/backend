"""Seed the mandatory platform fallback for payout-batch approval.

New tenants receive their own non-destructive copy during ledger provisioning, but
entities created before that hook existed need a last-resort route. One platform
template covers all of them through the workflow resolver's tenant to platform
cascade. Existing platform configuration is left untouched.

The reverse is deliberately a no-op. Once a payout instance has used this template,
its audit history protects the row from deletion, and removing the fallback would
reintroduce an operational gap on older tenants.
"""
from django.db import migrations


DOCUMENT_TYPE = "payments.payout_batch"
TEMPLATE_CODE = "standard"
HIGH_VALUE_THRESHOLD = 50_000_000


def seed_platform_payout_approval_fallback(apps, schema_editor):
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")

    template, created = WorkflowTemplate.objects.get_or_create(
        tenant=None,
        branch=None,
        document_type=DOCUMENT_TYPE,
        code=TEMPLATE_CODE,
        defaults={
            "name": "Payout-batch approval",
            "description": "Default approval rule for a payout batch.",
            "notification_events": {},
            "is_active": True,
        },
    )
    if not created:
        return

    WorkflowStage.objects.create(
        template=template,
        code="checker",
        label="Payout checker approval",
        kind="APPROVAL",
        order=10,
        approver_source="ROLE",
        approver_role_key="payout-approver",
        approver_scope="SCHOOL",
        advance_rule="ANY",
        quorum_count=0,
        on_rejection="TERMINAL",
        skip_if_no_approvers=False,
    )
    WorkflowStage.objects.create(
        template=template,
        code="senior",
        label="Senior payout approval",
        kind="APPROVAL",
        order=20,
        approver_source="ROLE",
        approver_role_key="payout-senior-approver",
        approver_scope="SCHOOL",
        advance_rule="ANY",
        quorum_count=0,
        on_rejection="TERMINAL",
        skip_if_no_approvers=False,
        inclusion_condition={
            "op": "gte",
            "field": "total_amount",
            "value": HIGH_VALUE_THRESHOLD,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0005_upgrade_seeded_payout_ladders"),
        ("vs_workflow", "0010_alter_workflowstage_skip_if_no_approvers"),
    ]

    operations = [
        migrations.RunPython(
            seed_platform_payout_approval_fallback,
            migrations.RunPython.noop,
        ),
    ]
