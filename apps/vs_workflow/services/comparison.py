"""Comparing a platform template with a tenant's own version of it.

The platform publishes one approval path; a tenant that adjusts it runs its
own. Editing the shared one then reaches only the tenants still following it,
which is a fact the person editing should be able to see rather than assume.

Everything here is read-only and pure: adoption counts and a structural diff,
computed from templates the caller has already been authorized to read.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from vs_workflow.models import WorkflowTemplate

# Stage settings worth reporting a difference on. Ordered as they are read on
# screen ("who approves it" before "how it advances"), because the list is
# rendered in this order and a diff is easier to trust when it matches the form.
STAGE_FIELDS = [
    ("label", "Label"),
    ("kind", "Kind"),
    ("order", "Position"),
    ("approver_source", "Approver source"),
    ("approver_role_key", "Approver role"),
    ("approver_scope", "Approver scope"),
    ("organogram_target", "Organogram target"),
    ("organogram_levels", "Levels up"),
    ("advance_rule", "Advance rule"),
    ("quorum_count", "Quorum"),
    ("on_rejection", "On rejection"),
    ("skip_if_no_approvers", "Auto-skip when nobody can approve"),
    ("inclusion_condition", "Applies when"),
]

TEMPLATE_FIELDS = [
    ("name", "Name"),
    ("description", "Description"),
    ("notification_events", "Notification events"),
]


def _stage_group_code(stage) -> Optional[str]:
    return stage.approver_group.code if stage.approver_group_id else None


def _rules_of(stage) -> List[Dict]:
    """A stage's dynamic rules as plain data, in evaluation order."""
    return [
        {"order": r.order, "condition": r.condition, "role_key": r.role_key,
         "label": r.label}
        for r in stage.dynamic_rules.all().order_by("order")
    ]


def _active_stages(template: WorkflowTemplate) -> Dict[str, object]:
    """Live stages by code. Retired stages are history, not configuration."""
    return {
        s.code: s
        for s in template.stages.filter(retired_at__isnull=True).order_by("order")
    }


def compare_templates(base: WorkflowTemplate, other: WorkflowTemplate) -> Dict:
    """How *other* differs from *base*, stage by stage.

    "Added" and "removed" are said from the reader's point of view: the reader
    owns *base* (the shared template), so a stage only the tenant has is one
    they added. Stages are matched by code, which is what the publish endpoint
    upserts on, so a renamed label reads as a changed field rather than as a
    stage removed and another added.
    """
    base_stages = _active_stages(base)
    other_stages = _active_stages(other)

    added, removed, changed = [], [], []

    for code, stage in other_stages.items():
        if code not in base_stages:
            added.append({"code": code, "label": stage.label})

    for code, stage in base_stages.items():
        if code not in other_stages:
            removed.append({"code": code, "label": stage.label})

    for code, base_stage in base_stages.items():
        other_stage = other_stages.get(code)
        if other_stage is None:
            continue
        fields = []
        for field, label in STAGE_FIELDS:
            left, right = getattr(base_stage, field), getattr(other_stage, field)
            if left != right:
                fields.append({"field": field, "label": label,
                               "base": left, "other": right})
        # The group is a foreign key; compare the code, which is what a template
        # publishes and what a reader recognises.
        if _stage_group_code(base_stage) != _stage_group_code(other_stage):
            fields.append({"field": "approver_group_code", "label": "Approver group",
                           "base": _stage_group_code(base_stage),
                           "other": _stage_group_code(other_stage)})
        base_rules, other_rules = _rules_of(base_stage), _rules_of(other_stage)
        if base_rules != other_rules:
            fields.append({"field": "dynamic_role_rules", "label": "Rule ladder",
                           "base": base_rules, "other": other_rules})
        if fields:
            changed.append({"code": code, "label": base_stage.label, "fields": fields})

    template_fields = [
        {"field": field, "label": label,
         "base": getattr(base, field), "other": getattr(other, field)}
        for field, label in TEMPLATE_FIELDS
        if getattr(base, field) != getattr(other, field)
    ]

    base_routes = [
        {"from": r.from_stage.code if r.from_stage_id else None,
         "to": r.to_stage.code if r.to_stage_id else None,
         "order": r.order, "condition": r.condition}
        for r in base.routes.all().order_by("order")
    ]
    other_routes = [
        {"from": r.from_stage.code if r.from_stage_id else None,
         "to": r.to_stage.code if r.to_stage_id else None,
         "order": r.order, "condition": r.condition}
        for r in other.routes.all().order_by("order")
    ]

    return {
        "template_fields": template_fields,
        "stages": {"added": added, "removed": removed, "changed": changed},
        "routes_differ": base_routes != other_routes,
        "identical": not (template_fields or added or removed or changed
                          or base_routes != other_routes),
    }


def adoption_for(template: WorkflowTemplate) -> Dict:
    """Who runs *template* as published, and who runs their own instead.

    Counts tenants, not templates: a tenant with both a branch-level and a
    tenant-level version of the same path has still adjusted it once, and
    reporting two would overstate the divergence.
    """
    from vs_tenants.models import Tenant

    customers = Tenant.objects.filter(
        kind__in=[Tenant.Kind.SCHOOL, Tenant.Kind.ORGANIZATION],
        status=Tenant.Status.ACTIVE,
    )

    own = (WorkflowTemplate.all_objects
           .filter(document_type=template.document_type, code=template.code,
                   is_active=True, tenant__isnull=False)
           .exclude(tenant__kind=Tenant.Kind.PLATFORM)
           .select_related("tenant")
           .order_by("tenant__name", "-updated_at"))

    adjusted, seen = [], set()
    for row in own:
        if row.tenant_id in seen:
            continue
        seen.add(row.tenant_id)
        adjusted.append({
            "tenant_slug": row.tenant.slug,
            "tenant_name": row.tenant.name,
            "template_id": row.pk,
            "branch": row.branch_id,
            "stage_count": row.stages.filter(retired_at__isnull=True).count(),
            "updated_at": row.updated_at,
        })

    total = customers.count()
    return {
        "customer_count": total,
        # Everyone who has not adjusted it runs the published version, including
        # tenants that have never opened it - that is what inheriting means.
        "following_count": max(total - len(adjusted), 0),
        "adjusted_count": len(adjusted),
        "adjusted": adjusted,
    }
