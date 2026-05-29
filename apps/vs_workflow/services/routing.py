"""
Routing and stage advancement — the core state machine.

advance_instance: moves the instance forward from its current stage.
_terminate_approved, _terminate_rejected, _return_to_requester: terminal transitions.
"""

from typing import Optional

from django.db import transaction
from django.utils import timezone

from vs_workflow.conditions import evaluate_condition
from vs_workflow.constants import (
    AuditEventType, StageKind, WorkflowInstanceStatus, WorkflowStageStatus,
)
from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.handlers import get_handler
from vs_workflow.models import (
    WorkflowInstance, WorkflowRoutePath, WorkflowStage,
    WorkflowStageApprover, WorkflowStageInstance,
)
from vs_workflow.services import approvers as approvers_service
from vs_workflow.services import audit as audit_service


def _pick_next_stage(instance: WorkflowInstance,
                     from_stage: Optional[WorkflowStage]) -> Optional[WorkflowStage]:
    """Return the next stage or None (terminate APPROVED)."""
    template = instance.template
    document = instance.document
    has_routes = WorkflowRoutePath.objects.filter(template=template).exists()

    if has_routes:
        route_qs = WorkflowRoutePath.objects.filter(
            template=template, from_stage=from_stage).order_by("order")
        chosen = None
        evaluations = []
        for route in route_qs:
            matches, trace = evaluate_condition(route.condition, document)
            evaluations.append({
                "route_id": str(route.id),
                "to_stage": route.to_stage.code if route.to_stage else "EXIT",
                "trace": trace, "picked": False,
            })
            if matches:
                chosen = route
                evaluations[-1]["picked"] = True
                break
        audit_service.write(instance, AuditEventType.ROUTE_EVALUATED, context={
            "from_stage": from_stage.code if from_stage else "ENTRY",
            "evaluations": evaluations,
        })
        if chosen is None and route_qs.exists():
            raise TemplateInvalidError(
                "No route matched and all routes had conditions.",
                from_stage=from_stage.code if from_stage else "ENTRY",
            )
        if chosen is not None:
            return chosen.to_stage
        # Fall through to linear logic if no routes matched (empty route_qs).

    stages = list(template.stages.order_by("order"))
    if not stages:
        raise TemplateInvalidError("Template has no stages", template=str(template.id))
    if from_stage is None:
        return stages[0]
    for idx, s in enumerate(stages):
        if s.pk == from_stage.pk:
            return stages[idx + 1] if idx + 1 < len(stages) else None
    raise TemplateInvalidError("Current stage not part of template.",
                               stage=from_stage.code, template=str(template.id))


def _readonly_next_stage(template, document, from_stage, stages, has_routes):
    """Pure (no-write) sibling of _pick_next_stage for previews.

    Raises TemplateInvalidError when routing is genuinely undecidable, so the
    caller can fall back to "moves forward" rather than guessing.
    """
    if has_routes:
        routes = list(
            WorkflowRoutePath.objects.filter(template=template, from_stage=from_stage).order_by("order")
        )
        if routes:
            for route in routes:
                matches, _ = evaluate_condition(route.condition, document)
                if matches:
                    return route.to_stage
            raise TemplateInvalidError("preview: no route matched")
        # No routes from this stage — fall through to linear order.
    if not stages:
        return None
    if from_stage is None:
        return stages[0]
    for idx, s in enumerate(stages):
        if s.pk == from_stage.pk:
            return stages[idx + 1] if idx + 1 < len(stages) else None
    return None


def preview_next_approval_stage(instance: WorkflowInstance):
    """Read-only, best-effort preview of the next APPROVAL stage that would run
    once the current stage completes.

    Returns ``{"label": str | None, "is_final": bool}`` or ``None`` when the
    instance isn't currently awaiting a decision. ``is_final=True`` means the
    workflow would be fully approved. A ``None`` label means the next step
    can't be determined without running (e.g. a conditional branch); callers
    should fall back to a generic "moves forward" message. Never writes.
    """
    if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
        return None
    from_stage = instance.current_stage
    if from_stage is None:
        return None

    template = instance.template
    document = instance.document
    try:
        stages = list(template.stages.order_by("order"))
        has_routes = WorkflowRoutePath.objects.filter(template=template).exists()
        cursor = from_stage
        for _ in range(50):  # mirror advance_instance's MAX_HOPS cycle guard
            nxt = _readonly_next_stage(template, document, cursor, stages, has_routes)
            if nxt is None:
                return {"label": None, "is_final": True}
            if nxt.retired_at is not None:
                cursor = nxt
                continue
            if nxt.kind == StageKind.BRANCH:
                cursor = nxt
                continue
            if nxt.kind == StageKind.APPROVAL and nxt.inclusion_condition:
                matches, _ = evaluate_condition(nxt.inclusion_condition, document)
                if not matches:
                    cursor = nxt
                    continue
            return {"label": nxt.label, "is_final": False}
        return {"label": None, "is_final": False}
    except Exception:
        return {"label": None, "is_final": False}


def _activate_stage(instance: WorkflowInstance, stage: WorkflowStage,
                    attempt: int) -> WorkflowStageInstance:
    stage_instance, _ = WorkflowStageInstance.objects.get_or_create(
        instance=instance, stage=stage, attempt=attempt,
        defaults={"status": WorkflowStageStatus.ACTIVE, "activated_at": timezone.now()},
    )
    if stage_instance.status != WorkflowStageStatus.ACTIVE:
        stage_instance.status = WorkflowStageStatus.ACTIVE
        stage_instance.activated_at = timezone.now()
        stage_instance.resolved_at = None
        stage_instance.save(update_fields=["status", "activated_at", "resolved_at"])

    eligible = approvers_service.resolve_approvers(stage, instance)
    WorkflowStageApprover.objects.bulk_create([
        WorkflowStageApprover(stage_instance=stage_instance, user=ea.user,
                              on_behalf_of=ea.on_behalf_of, attempt=attempt)
        for ea in eligible
    ])
    instance.current_stage = stage
    instance.save(update_fields=["current_stage", "updated_at"])
    audit_service.write(instance, AuditEventType.STAGE_ACTIVATED,
                        stage_instance=stage_instance, context={
                            "stage_code": stage.code, "stage_label": stage.label,
                            "attempt": attempt, "eligible_count": len(eligible),
                        })
    return stage_instance


def _skip_stage(instance: WorkflowInstance, stage: WorkflowStage, attempt: int,
                reason_event: AuditEventType, reason_detail: str = "") -> WorkflowStageInstance:
    si, _ = WorkflowStageInstance.objects.get_or_create(
        instance=instance, stage=stage, attempt=attempt,
        defaults={"status": WorkflowStageStatus.SKIPPED, "activated_at": timezone.now(),
                  "resolved_at": timezone.now(), "skip_reason": reason_detail},
    )
    if si.status != WorkflowStageStatus.SKIPPED:
        si.status = WorkflowStageStatus.SKIPPED
        si.resolved_at = timezone.now()
        si.skip_reason = reason_detail
        si.save(update_fields=["status", "resolved_at", "skip_reason"])
    audit_service.write(instance, reason_event, stage_instance=si, context={
        "stage_code": stage.code, "attempt": attempt, "detail": reason_detail,
    })
    return si


def advance_instance(instance: WorkflowInstance, *, current_attempt: int = 1) -> WorkflowInstance:
    """Move the instance forward, looping through auto-skip stages."""
    if instance.is_terminal:
        return instance
    MAX_HOPS = 50
    hops = 0
    from_stage = instance.current_stage

    while True:
        hops += 1
        if hops > MAX_HOPS:
            raise TemplateInvalidError("Route exceeded max hops; possible cycle.",
                                       template=str(instance.template_id))
        next_stage = _pick_next_stage(instance, from_stage)
        if next_stage is None:
            return _terminate_approved(instance)

        # Skip stages retired from the template (works for both linear order and
        # route targets). Live instances thus advance past a removed stage.
        if next_stage.retired_at is not None:
            _skip_stage(instance, next_stage, current_attempt,
                        AuditEventType.STAGE_SKIPPED_CONDITION, "stage_retired")
            from_stage = next_stage
            continue

        # Evaluate inclusion condition for APPROVAL stages.
        if next_stage.kind == StageKind.APPROVAL and next_stage.inclusion_condition:
            matches, trace = evaluate_condition(next_stage.inclusion_condition, instance.document)
            if not matches:
                _skip_stage(instance, next_stage, current_attempt,
                             AuditEventType.STAGE_SKIPPED_CONDITION, "inclusion_condition_false")
                from_stage = next_stage
                continue

        # BRANCH stages are routing-only — skip and re-evaluate.
        if next_stage.kind == StageKind.BRANCH:
            _skip_stage(instance, next_stage, current_attempt,
                         AuditEventType.STAGE_SKIPPED_CONDITION, "branch_node")
            from_stage = next_stage
            continue

        # APPROVAL stage — activate it.
        _activate_stage(instance, next_stage, current_attempt)
        eligible = approvers_service.resolve_approvers(next_stage, instance)
        if not eligible:
            if next_stage.skip_if_no_approvers:
                _skip_stage(instance, next_stage, current_attempt,
                             AuditEventType.STAGE_SKIPPED_NO_APPROVER, "zero_eligible_approvers")
                from_stage = next_stage
                continue
            # Block here — admins must intervene.
            audit_service.write(instance, AuditEventType.STAGE_ACTIVATED, context={
                "warning": "stage_active_with_no_approvers", "stage": next_stage.code,
            })

        instance.status = WorkflowInstanceStatus.IN_PROGRESS
        instance.state_version += 1
        instance.save(update_fields=["status", "state_version", "updated_at"])
        return instance


def _terminate_approved(instance: WorkflowInstance) -> WorkflowInstance:
    instance.status = WorkflowInstanceStatus.APPROVED
    instance.current_stage = None
    instance.completed_at = timezone.now()
    instance.state_version += 1
    instance.save(update_fields=["status", "current_stage", "completed_at",
                                  "state_version", "updated_at"])
    audit_service.write(instance, AuditEventType.INSTANCE_APPROVED)
    handler = get_handler(instance.document_type)
    handler.on_approved(instance, {"template": instance.template.code})
    return instance


def _terminate_rejected(instance: WorkflowInstance, actor, comment: str) -> WorkflowInstance:
    instance.status = WorkflowInstanceStatus.REJECTED
    instance.current_stage = None
    instance.completed_at = timezone.now()
    instance.state_version += 1
    instance.save(update_fields=["status", "current_stage", "completed_at",
                                  "state_version", "updated_at"])
    audit_service.write(instance, AuditEventType.INSTANCE_REJECTED,
                        actor=actor, context={"comment": comment})
    get_handler(instance.document_type).on_rejected(instance, {"comment": comment})
    return instance


def _return_to_requester(instance: WorkflowInstance, actor, comment: str,
                          returning_stage_id) -> WorkflowInstance:
    instance.status = WorkflowInstanceStatus.RETURNED
    instance.state_version += 1
    instance.save(update_fields=["status", "state_version", "updated_at"])
    audit_service.write(instance, AuditEventType.INSTANCE_RETURNED, actor=actor, context={
        "comment": comment, "returning_stage_id": str(returning_stage_id),
    })
    get_handler(instance.document_type).on_returned(instance, {"comment": comment})
    return instance


