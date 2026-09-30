"""Bring existing books up to the receivables guards.

Three things, each safe to run on books of any age:

* **Fee-run billing keys.** A live invoice raised from a fee structure (reference
  ``FEE:<code>``) is given that reference as its billing key, so the database's
  one-invoice-per-customer-per-key rule covers the bills raised before the rule
  existed and a re-run finds them. Where a customer already holds two live invoices
  from one structure (a double run from before the rule), neither is keyed: the
  duplicate is the bursar's to void, and keying one would make an arbitrary choice
  for them. The owner layer's own migration then adds the billing period each bill
  was raised for.
* **Cumulative approval steps.** A concession or credit-note step conditioned on
  the document's own amount (``amount`` / ``total``) is rewritten to weigh the
  running total of reductions on the same bill and billing period
  (``cumulative_amount``), which is what the models now expose as their approval
  amount. Only that one field name changes; the operator and threshold stand.
* **Customer credit transfer routes.** Every tenant holding its own adjustment
  routes (the books publish them) is given an empty route for customer credit
  transfers, as new books are. With no steps, a transfer cannot be submitted until
  the tenant adds its approvers (or runs ``seed_finance_approvals``), which is the
  intended end state: moving one customer's money to another is never approved by
  default.

The reverse undoes the second and third; the keys are left, since they only name
what each bill already was.
"""
from django.db import migrations
from django.db.models import Count

_LIVE_EXCLUDED = ("REVERSED", "CANCELLED")
_THRESHOLD_TYPES = {"finance.concession": "amount", "finance.credit_note": "total"}
_CUMULATIVE = "cumulative_amount"
_TRANSFER_TYPE = "finance.customer_credit_transfer"
_TEMPLATE_CODE = "standard"


def _rewrite(condition, old, new):
    """``condition`` with every ``{"field": old}`` leaf renamed to ``new``."""
    if isinstance(condition, dict):
        out = {}
        for key, value in condition.items():
            if key == "field" and value == old:
                out[key] = new
            else:
                out[key] = _rewrite(value, old, new)
        return out
    if isinstance(condition, list):
        return [_rewrite(item, old, new) for item in condition]
    return condition


def forwards(apps, schema_editor):
    Invoice = apps.get_model("vs_finance", "Invoice")
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    live = Invoice.objects.filter(reference__startswith="FEE:").exclude(status__in=_LIVE_EXCLUDED)
    single = (
        live.values("entity_id", "customer_id", "reference")
        .annotate(n=Count("id")).filter(n=1)
    )
    for row in single.iterator():
        live.filter(
            entity_id=row["entity_id"], customer_id=row["customer_id"],
            reference=row["reference"], billing_key="",
        ).update(billing_key=row["reference"])

    for document_type, field in _THRESHOLD_TYPES.items():
        for stage in WorkflowStage.objects.filter(
                template__document_type=document_type, inclusion_condition__isnull=False):
            rewritten = _rewrite(stage.inclusion_condition, field, _CUMULATIVE)
            if rewritten != stage.inclusion_condition:
                stage.inclusion_condition = rewritten
                stage.save(update_fields=["inclusion_condition"])

    tenants = set(
        WorkflowTemplate.objects.filter(
            document_type="finance.refund", branch__isnull=True, code=_TEMPLATE_CODE,
            tenant__isnull=False,
        ).values_list("tenant_id", flat=True)
    )
    for tenant_id in sorted(tenants):
        WorkflowTemplate.objects.get_or_create(
            tenant_id=tenant_id, branch=None, document_type=_TRANSFER_TYPE, code=_TEMPLATE_CODE,
            defaults={
                "name": "Customer credit transfer approval",
                "description": (
                    "Approval route for a customer credit transfer, held by this tenant so "
                    "its transfers are never governed by shared platform rules. The steps "
                    "are the tenant's own to add."
                ),
            },
        )


def backwards(apps, schema_editor):
    WorkflowStage = apps.get_model("vs_workflow", "WorkflowStage")
    WorkflowTemplate = apps.get_model("vs_workflow", "WorkflowTemplate")

    for document_type, field in _THRESHOLD_TYPES.items():
        for stage in WorkflowStage.objects.filter(
                template__document_type=document_type, inclusion_condition__isnull=False):
            restored = _rewrite(stage.inclusion_condition, _CUMULATIVE, field)
            if restored != stage.inclusion_condition:
                stage.inclusion_condition = restored
                stage.save(update_fields=["inclusion_condition"])
    empty = WorkflowTemplate.objects.filter(
        document_type=_TRANSFER_TYPE, code=_TEMPLATE_CODE, branch__isnull=True,
    ).exclude(stages__isnull=False)
    empty.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_finance", "0040_ar_guards"),
        ("vs_workflow", "0007_workflowtemplate_is_active_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
