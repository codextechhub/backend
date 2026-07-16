"""Audit log write helper — thin wrapper so callers never import the model directly."""
from typing import Optional
from vs_workflow.constants import AuditEventType
from vs_workflow.models import WorkflowAuditLog, WorkflowInstance, WorkflowStageInstance

# Append one workflow audit row inside the caller's transaction.
def write(instance: WorkflowInstance, event_type: AuditEventType, *,
          actor=None, stage_instance: Optional[WorkflowStageInstance]=None,
          context: Optional[dict]=None, message: str="") -> WorkflowAuditLog:
    """Append a single immutable audit entry to the instance's log.

    WorkflowAuditLog rows are never updated or deleted — callers must never
    call write() inside a rollback-only savepoint because the audit entry
    would disappear with the savepoint. The log is the source of truth for
    "who did what and when" across the whole workflow lifecycle.
    """
    from vs_tenants.context import add_proxy_audit_metadata, resolve_audit_identity

    actor, effective_user, proxy_session = resolve_audit_identity(actor)
    context = add_proxy_audit_metadata(context, effective_user, proxy_session)
    return WorkflowAuditLog.objects.create(
        instance=instance, event_type=event_type, actor=actor,
        stage_instance=stage_instance, context=context or {}, message=message,
    )
