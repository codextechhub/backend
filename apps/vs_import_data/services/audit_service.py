from __future__ import annotations

from vs_audit.models import AuditModuleKey, AuditActionType
from vs_audit.services import emit_audit_event

# Maps the string action labels used internally in the import pipeline to
# the canonical AuditActionType choices stored in AuditEvent.
_ACTION_MAP: dict[str, str] = {
    # Template lifecycle
    "template_created": AuditActionType.CREATE,
    # Batch lifecycle
    "batch_uploaded": AuditActionType.DATA_FILE_UPLOADED,
    "batch_updated": AuditActionType.UPDATE,
    "batch_cancelled": AuditActionType.UPDATE,
    "batch_deleted": AuditActionType.DELETE,
    # Validation
    "batch_validated": AuditActionType.CUSTOM,
    # Issues
    "issue_resolved": AuditActionType.UPDATE,
    # Import execution
    "import_triggered": AuditActionType.DATA_IMPORT_STARTED,
    "import_row_success": AuditActionType.DATA_IMPORT_ROW_PROCESSED,
    "import_row_skipped": AuditActionType.DATA_IMPORT_ROW_PROCESSED,
    "import_completed": AuditActionType.DATA_IMPORT_COMPLETED,
    "import_failed": AuditActionType.DATA_IMPORT_FAILED,
    "import_rollback": AuditActionType.DATA_IMPORT_ROLLED_BACK,
}


def create_import_audit_log(
    *,
    school=None,
    branch=None,
    action: str,
    actor=None,
    import_batch=None,
    job=None,
    entity_type: str = "",
    entity_id: str = "",
    before_data: dict | None = None,
    after_data: dict | None = None,
    message: str = "",
    metadata: dict | None = None,
):
    """
    Record one import-pipeline action as a vs_audit AuditEvent.

    The function signature is kept stable so existing callers (import_executor,
    rollback_service) do not need to change. Import-specific context (branch,
    batch, job) is forwarded into the event's metadata.
    """
    from vs_audit.services import AuditDiffService

    before_data = before_data or {}
    after_data = after_data or {}
    metadata = metadata or {}

    diff_data = AuditDiffService.diff_dicts(before_data, after_data)

    action_type = _ACTION_MAP.get(action, AuditActionType.CUSTOM)

    extra_meta = {
        "import_action": action,
        "school_id": str(school.pk) if school else None,
        "branch_id": str(branch.pk) if branch else None,
        "import_batch_id": str(import_batch.pk) if import_batch else None,
        "job_id": str(job.pk) if job else None,
        "message": message,
        **metadata,
    }

    # Imports run inside Celery tasks (see vs_import_data.tasks), where there is
    # no request and therefore no ambient tenant to inherit. Every row a
    # 900-pupil import writes would otherwise be unattributable to the school it
    # was written for. Both references carry the tenant directly; the branch is
    # preferred because a branch-scoped import names it precisely.
    tenant = None
    if branch is not None:
        tenant = branch.tenant
    elif school is not None:
        tenant = school.tenant

    return emit_audit_event(
        module_key=AuditModuleKey.IMPORT,
        action_type=action_type,
        actor_user=actor,
        tenant=tenant,
        entity_type=entity_type or "ImportBatch",
        entity_id=str(entity_id) if entity_id else (str(import_batch.pk) if import_batch else ""),
        entity_label=str(import_batch) if import_batch else "",
        before_data=before_data,
        diff_data=diff_data,
        metadata=extra_meta,
    )
