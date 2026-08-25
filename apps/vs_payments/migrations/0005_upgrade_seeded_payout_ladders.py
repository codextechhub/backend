"""Restore senior approval only on the recognizably seeded payout ladder.

Tenant administrators may customize payout templates, so a broad update by document
type would overwrite deliberate policy. This migration upgrades only the exact shipped
one-stage definition and leaves every other shape untouched.
"""
from django.db import migrations


DOCUMENT_TYPE = "payments.payout_batch"
TEMPLATE_CODE = "standard"
CHECKER_ROLE = "payout-approver"
SENIOR_ROLE = "payout-senior-approver"
HIGH_VALUE_THRESHOLD = 50_000_000


def _is_seeded_template(template, WorkflowRoutePath) -> bool:
    """Recognize the shipped one-stage row without guessing at custom templates."""
    if template.name != "Payout-batch approval":
        return False
    if template.description not in {
        "Default approval rule for a payout batch.",
        "Approval rule for a payout batch.",
    }:
        return False
    if WorkflowRoutePath.objects.filter(template_id=template.pk).exists():
        return False
    stages = list(template.stages.all())
    if len(stages) != 1:
        return False
    stage = stages[0]
    return (
        stage.retired_at is None
        and stage.code == "approver"
        and stage.label == "Payout approval"
        and stage.kind == "APPROVAL"
        and stage.order == 10
        and stage.approver_source == "ROLE"
        and stage.approver_role_key == CHECKER_ROLE
        and stage.approver_scope == "SCHOOL"
        and stage.advance_rule == "ANY"
        and stage.quorum_count == 0
        and stage.on_rejection == "TERMINAL"
        and stage.skip_if_no_approvers is False
        and stage.inclusion_condition is None
    )


def _senior_role(TenantRoleTemplate, tenant_id):
    """Create the holder-less senior role required by one affected tenant."""
    role = TenantRoleTemplate.objects.filter(
        tenant_id=tenant_id, key=SENIOR_ROLE,
    ).first()
    if role is not None:
        return role
    display_name = "Payout Senior Approver"
    if TenantRoleTemplate.objects.filter(
        tenant_id=tenant_id, name=display_name,
    ).exists():
        display_name = SENIOR_ROLE
    return TenantRoleTemplate.objects.create(
        tenant_id=tenant_id,
        key=SENIOR_ROLE,
        name=display_name,
        description=(
            "Approves high-value payout batches. Nobody holds it until an "
            "administrator assigns someone."
        ),
        status="ACTIVE",
    )


def upgrade_seeded_ladders(apps, schema_editor):
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowRoutePath = apps.get_model("vs_workflow", "WorkflowRoutePath")
    TenantRoleTemplate = apps.get_model("vs_rbac", "TenantRoleTemplate")

    templates = WorkflowTemplate.objects.filter(
        document_type=DOCUMENT_TYPE, code=TEMPLATE_CODE,
    ).prefetch_related("stages")
    for template in templates:
        if not _is_seeded_template(template, WorkflowRoutePath):
            continue
        role = (
            _senior_role(TenantRoleTemplate, template.tenant_id)
            if template.tenant_id is not None else None
        )
        WorkflowStage.objects.create(
            template_id=template.pk,
            code="senior",
            label="Senior payout approval",
            kind="APPROVAL",
            order=20,
            approver_source="ROLE",
            approver_scope="SCHOOL",
            approver_role_key=SENIOR_ROLE,
            approver_role_id=getattr(role, "pk", None),
            advance_rule="ANY",
            quorum_count=0,
            on_rejection="TERMINAL",
            skip_if_no_approvers=False,
            inclusion_condition={
                "op": "gte", "field": "total_amount", "value": HIGH_VALUE_THRESHOLD,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0004_payoutbatch_idempotency_key_and_more"),
        ("vs_rbac", "0009_remove_restricted_grant_bypasses"),
        ("vs_workflow", "0010_alter_workflowstage_skip_if_no_approvers"),
    ]

    operations = [
        migrations.RunPython(upgrade_seeded_ladders, migrations.RunPython.noop),
    ]
