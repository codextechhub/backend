"""Template publishing - create or update in place."""

from typing import Optional

from django.db import transaction
from django.utils import timezone

from vs_workflow.conditions import validate_condition
from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.models import WorkflowInstance, WorkflowTemplate


# Resolve organogram position references without requiring vs_user in RBAC-only installs.
def _resolve_position(code: Optional[str]):
    """Resolve a CX organogram Position by its code, or None.

    Used only by ORGANOGRAM/SPECIFIC_POSITION stages. Degrades to None if the
    code is blank or vs_user is unavailable, so RBAC-only installs are unaffected.
    """
    if not code:
        return None
    try:
        from vs_user.models import Position
    except ImportError:
        return None
    return Position.objects.filter(code=code).first()


# Resolve a ROLE stage's role key, anchoring it when the template has a tenant.
def _resolve_role(stage_payload: dict, tenant):
    """Validate approver_role_key and return the matching role, or None.

    Resolution at run time goes through the key, so a central (tenant-less)
    template can name the same authority in every tenant. The foreign key is
    only an anchor for tenant-scoped templates, where the role can be checked
    to exist now and protected from deletion later.

    A tenant-scoped stage naming a role that does not exist is a mistake worth
    failing the publish for. A central stage cannot be checked that way - the
    tenants that will run it may not even exist yet - so its key is accepted
    and ``check_workflow_role_coverage`` reports the gaps instead.
    """
    if stage_payload.get("approver_source") != "ROLE":
        return None
    key = stage_payload.get("approver_role_key") or ""
    label = stage_payload.get("code") or stage_payload.get("label") or "?"
    if not key:
        raise TemplateInvalidError(
            f"Stage '{label}': approver_role_key is required when approver_source is ROLE.")
    if tenant is None:
        return None

    from vs_rbac.models import TenantRoleTemplate

    role = TenantRoleTemplate.objects.filter(
        tenant=tenant, key=key, status=TenantRoleTemplate.Status.ACTIVE,
    ).first()
    if role is None:
        raise TemplateInvalidError(
            f"Stage '{label}': no active role with key '{key}' exists in this tenant.")
    return role


# Resolve a WORKFLOW_GROUP stage's group code to the tenant's group.
def _resolve_group(stage_payload: dict, tenant):
    """Resolve approver_group_code to the tenant's WorkflowApproverGroup.

    Fails the publish rather than degrading to None, for the same reason as
    _resolve_role: a group stage that lost its group would auto-skip (or
    stall) every future instance.
    """
    if stage_payload.get("approver_source") != "WORKFLOW_GROUP":
        return None
    code = stage_payload.get("approver_group_code") or ""
    label = stage_payload.get("code") or stage_payload.get("label") or "?"
    if not code:
        raise TemplateInvalidError(
            f"Stage '{label}': approver_group_code is required when "
            "approver_source is WORKFLOW_GROUP.")
    if tenant is None:
        raise TemplateInvalidError(
            f"Stage '{label}': WORKFLOW_GROUP stages need a tenant-scoped template - "
            "global templates cannot reference tenant approver groups.")

    from vs_workflow.models import WorkflowApproverGroup

    group = WorkflowApproverGroup.all_objects.filter(
        tenant=tenant, code=code, is_active=True,
    ).first()
    if group is None:
        raise TemplateInvalidError(
            f"Stage '{label}': no active approver group with code '{code}' "
            "exists in this tenant.")
    return group


# Validate and resolve the rules of a DYNAMIC_ROLE stage.
def _parse_dynamic_rules(stage_payload: dict, tenant):
    """Turn the payload's dynamic_role_rules into resolved, ordered rule specs.

    Everything that would make a rule set unusable is rejected here rather than
    at approval time: an unknown or inactive role, a malformed condition, no
    rules at all, or rules sitting after the fallback where they could never
    fire. The list comes back in evaluation order.
    """
    if stage_payload.get("approver_source") != "DYNAMIC_ROLE":
        return None

    label = stage_payload.get("code") or stage_payload.get("label") or "?"
    rules = stage_payload.get("dynamic_role_rules") or []
    if not isinstance(rules, list) or not rules:
        raise TemplateInvalidError(
            f"Stage '{label}': dynamic_role_rules is required when "
            "approver_source is DYNAMIC_ROLE.")

    from vs_rbac.models import TenantRoleTemplate

    parsed = []
    for i, raw in enumerate(rules):
        where = f"Stage '{label}' rule {i + 1}"
        if not isinstance(raw, dict):
            raise TemplateInvalidError(f"{where}: each rule must be an object.")
        key = raw.get("role_key") or ""
        if not key:
            raise TemplateInvalidError(f"{where}: 'role_key' is required.")
        role = None
        if tenant is not None:
            role = TenantRoleTemplate.objects.filter(
                tenant=tenant, key=key, status=TenantRoleTemplate.Status.ACTIVE,
            ).first()
            if role is None:
                raise TemplateInvalidError(
                    f"{where}: no active role with key '{key}' exists in this tenant.")
        condition = raw.get("condition")
        validate_condition(condition, where)
        parsed.append({
            "order": raw.get("order", i),
            "condition": condition,
            "role_key": key,
            "role": role,
            "label": raw.get("label", "") or "",
        })

    parsed.sort(key=lambda r: r["order"])
    for i, rule in enumerate(parsed):
        if rule["condition"] in (None, {}) and i != len(parsed) - 1:
            raise TemplateInvalidError(
                f"Stage '{label}': the fallback rule (no condition) must be last - "
                f"{len(parsed) - i - 1} rule(s) after it could never match.")
    # Re-index so stored order is dense and matches evaluation order.
    for i, rule in enumerate(parsed):
        rule["order"] = i
    return parsed


# Publish one workflow template definition atomically.
@transaction.atomic
def publish_template(*, tenant, branch=None, document_type: str, code: str, name: str,
                     description: str = "", notification_events: Optional[dict] = None,
                     created_by=None, stages_payload: Optional[list] = None,
                     routes_payload: Optional[list] = None) -> WorkflowTemplate:
    """
    Create or update a workflow template in place.
    - On first publish: creates the template and its stages/routes.
    - On subsequent publishes: updates top-level fields, upserts stages by code,
      and replaces all routes.
    """
    from vs_workflow.models import (
        WorkflowRoutePath, WorkflowStage, WorkflowStageDynamicRule,
    )

    # Parse and validate every stage's dynamic rules before writing anything.
    # publish_template is atomic, but failing up front keeps the error message
    # about the payload rather than about a half-built template.
    dynamic_by_code = {}
    for s in (stages_payload or []):
        parsed = _parse_dynamic_rules(s, tenant)
        if parsed is not None:
            dynamic_by_code[s["code"]] = parsed
        # Stage inclusion conditions are checked here for the same reason the
        # dynamic ones are: a bad operator otherwise surfaces mid-approval.
        validate_condition(s.get("inclusion_condition"),
                           f"Stage '{s.get('code')}' inclusion_condition")
    for i, r in enumerate(routes_payload or []):
        validate_condition(r.get("condition"), f"Route {i + 1} condition")

    template, created = WorkflowTemplate.objects.select_for_update().get_or_create(
        tenant=tenant, branch=branch, document_type=document_type, code=code,
        defaults={
            "name": name, "description": description,
            "notification_events": notification_events or {},
            "created_by": created_by,
        },
    )

    if not created:
        # Top-level template metadata is updated in place so references remain stable.
        template.name = name
        template.description = description
        template.notification_events = notification_events or {}
        template.save(update_fields=["name", "description", "notification_events", "updated_at"])

    # Upsert stages by code. The payload is the desired ACTIVE set: stages in it
    # are created/updated (and un-retired if previously removed); existing stages
    # absent from it are soft-retired - never hard-deleted, since running
    # instances reference them (FK is PROTECT). The engine skips retired stages
    # in all future routing, so live instances are unaffected.
    stage_by_code = {}
    payload_codes = []
    for s in (stages_payload or []):
        payload_codes.append(s["code"])
        defaults = {
            "label": s["label"],
            "kind": s.get("kind", "APPROVAL"),
            "order": s.get("order", 0),
            # Approver-source strategy. Defaults to the original RBAC path so
            # existing template payloads keep working unchanged.
            "approver_source": s.get("approver_source", "ROLE"),
            "approver_scope": s.get("approver_scope", "SCHOOL"),
            # Role config - only meaningful when approver_source==ROLE.
            "approver_role_key": s.get("approver_role_key", "") or "",
            "approver_role": _resolve_role(s, tenant),
            # Group config - only meaningful when approver_source==WORKFLOW_GROUP.
            "approver_group": _resolve_group(s, tenant),
            # Organogram config - only meaningful when approver_source==ORGANOGRAM.
            "organogram_target": s.get("organogram_target", ""),
            "organogram_levels": s.get("organogram_levels", 1),
            "organogram_position": _resolve_position(s.get("organogram_position_code")),
            "advance_rule": s.get("advance_rule", "UNANIMOUS"),
            "quorum_count": s.get("quorum_count", 0),
            "on_rejection": s.get("on_rejection", "TERMINAL"),
            "skip_if_no_approvers": s.get("skip_if_no_approvers", True),
            "inclusion_condition": s.get("inclusion_condition"),
            "retired_at": None,  # Re-including a stage code reactivates it for future routing.
        }
        stage, _ = WorkflowStage.objects.update_or_create(
            template=template, code=s["code"], defaults=defaults,
        )
        stage_by_code[s["code"]] = stage

        # Dynamic rules carry no instance-level references, so they are replaced
        # wholesale on every publish, exactly like routes. A stage that is no
        # longer DYNAMIC_ROLE loses its stale rules rather than keeping them
        # dormant and confusing the next reader.
        stage.dynamic_rules.all().delete()
        for rule in dynamic_by_code.get(s["code"], []):
            WorkflowStageDynamicRule.objects.create(
                stage=stage, order=rule["order"], condition=rule["condition"],
                role_key=rule["role_key"], role=rule["role"], label=rule["label"],
            )

    # Soft-retire stages the new payload no longer includes.
    (template.stages
     .exclude(code__in=payload_codes)
     .filter(retired_at__isnull=True)
     .update(retired_at=timezone.now()))

    # Replace routes entirely - they carry no instance-level FK references.
    WorkflowRoutePath.objects.filter(template=template).delete()
    for r in (routes_payload or []):
        from_code = r.get("from_stage_code")
        to_code = r.get("to_stage_code")
        WorkflowRoutePath.objects.create(
            template=template,
            from_stage=stage_by_code.get(from_code) if from_code else None,
            to_stage=stage_by_code.get(to_code) if to_code else None,
            order=r.get("order", 0),
            condition=r.get("condition"),
        )

    return template


# Find non-terminal work still tied to a template.
def active_instances_for_template(template: WorkflowTemplate) -> "QuerySet[WorkflowInstance]":
    """Return all non-terminal instances currently running against this template.

    Used before retiring or replacing a template to surface live work that
    would be affected. Callers should warn the admin rather than blocking -
    in-flight instances continue using their snapshotted stage definitions
    even after a new publish.
    """
    return WorkflowInstance.objects.active().filter(template=template)
