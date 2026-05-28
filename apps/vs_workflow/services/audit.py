"""Audit log write helper."""
from typing import Optional
from vs_workflow.constants import AuditEventType
from vs_workflow.models import WorkflowAuditLog, WorkflowInstance, WorkflowStageInstance

def write(instance: WorkflowInstance, event_type: AuditEventType, *,
          actor=None, stage_instance: Optional[WorkflowStageInstance]=None,
          context: Optional[dict]=None, message: str="") -> WorkflowAuditLog:
    return WorkflowAuditLog.objects.create(
        instance=instance, event_type=event_type, actor=actor,
        stage_instance=stage_instance, context=context or {}, message=message,
    )
