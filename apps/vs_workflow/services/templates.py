"""Template publishing — create or update in place."""

from typing import Optional

from django.db import transaction
from django.utils import timezone

from vs_workflow.models import WorkflowInstance, WorkflowTemplate


@transaction.atomic
def publish_template(*, school, branch=None, document_type: str, code: str, name: str,
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
        school=school, branch=branch, document_type=document_type, code=code,
        defaults={
            "name": name, "description": description,
            "notification_events": notification_events or {},
            "created_by": created_by,
        },
    )

    if not created:
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
        stage, _ = WorkflowStage.objects.update_or_create(
            template=template, code=s["code"],
            defaults={
                "label": s["label"],
                "kind": s.get("kind", "APPROVAL"),
                "order": s.get("order", 0),
                "approver_permission_key": s.get("approver_permission_key", ""),
                "approver_scope": s.get("approver_scope", "SCHOOL"),
                "advance_rule": s.get("advance_rule", "UNANIMOUS"),
                "quorum_count": s.get("quorum_count", 0),
                "on_rejection": s.get("on_rejection", "TERMINAL"),
                "skip_if_no_approvers": s.get("skip_if_no_approvers", True),
                "inclusion_condition": s.get("inclusion_condition"),
                "retired_at": None,  # (re)including a code reactivates it
            },
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


def active_instances_for_template(template: WorkflowTemplate) -> "QuerySet[WorkflowInstance]":
    return WorkflowInstance.objects.active().filter(template=template)
