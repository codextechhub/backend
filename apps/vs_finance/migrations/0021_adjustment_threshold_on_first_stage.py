"""Put the concession / credit-note threshold on the first stage of ladders
already seeded without it.

The seeded threshold-gated ladder used to carry its condition on the *second*
stage only, leaving the first unconditional. Something therefore always applied,
so ``approval_required`` gated a concession at every amount and a small goodwill
allowance was refused at ``/post/`` exactly as a large waiver was - the opposite
of what the threshold is for. ``_stages_payload`` now conditions both stages, but
``ensure_tenant_approval_templates`` is deliberately non-destructive and will
never revisit a ladder it already published, so every tenant seeded before this
would keep the old behaviour forever.

**Only the untouched seeded shape is repaired.** A ladder is rewritten only when
its first stage is the seeded one (code ``approver``, no condition of its own) and
its senior stage still carries the exact ``gte`` condition the seed writes. An
administrator who changed either is left alone - their configuration is a
decision, not a bug.
"""
from django.db import migrations

_SEEDED_TEMPLATE_CODE = "standard"
_THRESHOLD_GATED_TYPES = ("finance.concession", "finance.credit_note")


def _seeded_threshold(condition):
    """The threshold in a stage condition, or None if it is not the seeded shape."""
    if not isinstance(condition, dict):
        return None
    if condition.get("op") != "gte" or not condition.get("field"):
        return None
    if set(condition) != {"op", "field", "value"}:
        return None
    return condition


# Copy the senior stage's threshold onto the untouched first stage.
def apply_threshold_to_first_stage(apps, schema_editor):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    templates = WorkflowTemplate.objects.filter(
        code=_SEEDED_TEMPLATE_CODE, document_type__in=_THRESHOLD_GATED_TYPES,
    )
    for template in templates:
        stages = {s.code: s for s in WorkflowStage.objects.filter(
            template=template, retired_at__isnull=True)}
        first, senior = stages.get("approver"), stages.get("senior")
        if first is None or senior is None or len(stages) != 2:
            continue  # Not the seeded ladder any more.
        if first.inclusion_condition:
            continue  # Already conditioned, by this migration or by an admin.
        condition = _seeded_threshold(senior.inclusion_condition)
        if condition is None:
            continue  # An admin rewrote the senior condition; leave the pair alone.
        first.inclusion_condition = dict(condition)
        first.save(update_fields=["inclusion_condition"])


# Restore the unconditional first stage on ladders that still match the new shape.
def clear_threshold_from_first_stage(apps, schema_editor):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    templates = WorkflowTemplate.objects.filter(
        code=_SEEDED_TEMPLATE_CODE, document_type__in=_THRESHOLD_GATED_TYPES,
    )
    for template in templates:
        stages = {s.code: s for s in WorkflowStage.objects.filter(
            template=template, retired_at__isnull=True)}
        first, senior = stages.get("approver"), stages.get("senior")
        if first is None or senior is None or len(stages) != 2:
            continue
        # Only undo the exact pairing this migration creates.
        if first.inclusion_condition != senior.inclusion_condition:
            continue
        if _seeded_threshold(first.inclusion_condition) is None:
            continue
        first.inclusion_condition = None
        first.save(update_fields=["inclusion_condition"])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0020_alter_financeaccountmapping_key"),
        ("vs_workflow", "0007_workflowtemplate_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(apply_threshold_to_first_stage,
                             clear_threshold_from_first_stage),
    ]
