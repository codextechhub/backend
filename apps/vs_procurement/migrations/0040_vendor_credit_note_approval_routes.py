"""Give every existing approval scope a vendor credit note route, shaped like its bill route.

A credit note is approved the way the bill it corrects is. New books receive the
route from the procurement provisioner (``provision_approval_ladders``), which now
names the credit note among its document types; books created earlier have a bill
route and no credit note route, so a credit note submitted there would resolve to
nothing at all.

For each scope (platform, tenant or branch) holding a ``standard`` vendor-invoice
route, this publishes a ``standard`` vendor-credit-note route with a copy of that
route's live stages, their dynamic rules and the routes between them. A scope whose
bill route carries no stages gets an empty credit note route, which the engine
treats as unconfigured and refuses until somebody confirms, exactly as it does for
the bill. A scope that already has a credit note route keeps it untouched.

The reverse is a no-op: a published route may already have run an approval, whose
history protects it, and removing it would leave credit notes with no route.
"""
from django.db import migrations

SOURCE_TYPE = "procurement.vendor_invoice"
DOCUMENT_TYPE = "procurement.vendor_credit_note"
TEMPLATE_CODE = "standard"


def _clone(model, row, **overrides):
    """Create a copy of ``row`` with a fresh primary key and ``overrides`` applied."""
    data = {
        field.attname: getattr(row, field.attname)
        for field in model._meta.concrete_fields
        if not field.primary_key and field.name not in overrides
    }
    data.update(overrides)
    return model.objects.create(**data)


def publish_credit_note_routes(apps, schema_editor):
    Template = apps.get_model("vs_workflow", "WorkflowTemplate")
    Stage = apps.get_model("vs_workflow", "WorkflowStage")
    Rule = apps.get_model("vs_workflow", "WorkflowStageDynamicRule")
    Route = apps.get_model("vs_workflow", "WorkflowRoutePath")

    for source in Template.objects.filter(document_type=SOURCE_TYPE, code=TEMPLATE_CODE):
        if Template.objects.filter(
            tenant_id=source.tenant_id, branch_id=source.branch_id,
            document_type=DOCUMENT_TYPE, code=TEMPLATE_CODE,
        ).exists():
            continue
        target = Template.objects.create(
            tenant_id=source.tenant_id, branch_id=source.branch_id,
            document_type=DOCUMENT_TYPE, code=TEMPLATE_CODE,
            name="Vendor-credit-note approval",
            description="Approval route for a vendor credit note, copied from the bill route.",
            notification_events=source.notification_events or {},
            is_active=source.is_active, created_by_id=source.created_by_id,
        )
        stage_map = {}
        for stage in Stage.objects.filter(
            template_id=source.pk, retired_at__isnull=True,
        ).order_by("order", "pk"):
            copy = _clone(Stage, stage, template=target)
            stage_map[stage.pk] = copy
            for rule in Rule.objects.filter(stage_id=stage.pk).order_by("order", "pk"):
                _clone(Rule, rule, stage=copy)
        for route in Route.objects.filter(template_id=source.pk).order_by("order", "pk"):
            if route.from_stage_id is not None and route.from_stage_id not in stage_map:
                continue
            if route.to_stage_id is not None and route.to_stage_id not in stage_map:
                continue
            _clone(
                Route, route, template=target,
                from_stage=stage_map.get(route.from_stage_id),
                to_stage=stage_map.get(route.to_stage_id),
            )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0039_vendor_credit_notes_and_goods_returns"),
        ("vs_workflow", "0021_who_really_acted_under_a_proxy"),
    ]

    operations = [
        migrations.RunPython(publish_credit_note_routes, migrations.RunPython.noop),
    ]
