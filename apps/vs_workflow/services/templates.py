"""Template publishing — create or update in place."""

from typing import Optional

from django.db import transaction
from django.utils import timezone

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
    from vs_workflow.models import WorkflowRoutePath, WorkflowStage

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
    # absent from it are soft-retired — never hard-deleted, since running
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
            "approver_source": s.get("approver_source", "RBAC_PERMISSION"),
            "approver_permission_key": s.get("approver_permission_key", ""),
            "approver_scope": s.get("approver_scope", "SCHOOL"),
            # Organogram config — only meaningful when approver_source==ORGANOGRAM.
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

    # Soft-retire stages the new payload no longer includes.
    (template.stages
     .exclude(code__in=payload_codes)
     .filter(retired_at__isnull=True)
     .update(retired_at=timezone.now()))

    # Replace routes entirely — they carry no instance-level FK references.
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
    would be affected. Callers should warn the admin rather than blocking —
    in-flight instances continue using their snapshotted stage definitions
    even after a new publish.
    """
    return WorkflowInstance.objects.active().filter(template=template)
