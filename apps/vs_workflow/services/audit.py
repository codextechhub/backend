"""Audit log write helper — thin wrapper so callers never import the model directly."""
from typing import Optional
from vs_workflow.constants import AuditEventType
from vs_workflow.models import WorkflowAuditLog, WorkflowInstance, WorkflowStageInstance

def write(instance: WorkflowInstance, event_type: AuditEventType, *,
          actor=None, stage_instance: Optional[WorkflowStageInstance]=None,
          context: Optional[dict]=None, message: str="") -> WorkflowAuditLog:
    """Append a single immutable audit entry to the instance's log.

    WorkflowAuditLog rows are never updated or deleted — callers must never
    call write() inside a rollback-only savepoint because the audit entry
    would disappear with the savepoint. The log is the source of truth for
    "who did what and when" across the whole workflow lifecycle.
    """
    return WorkflowAuditLog.objects.create(
        instance=instance, event_type=event_type, actor=actor,
        stage_instance=stage_instance, context=context or {}, message=message,
    )
