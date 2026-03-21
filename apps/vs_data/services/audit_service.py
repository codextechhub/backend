from __future__ import annotations

from vs_audit.services import AuditDiffService
from ..models import ImportAuditLog


def create_import_audit_log(
    *,
    branch,
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
    Save one import-related audit log entry.
    """
    before_data = before_data or {}
    after_data = after_data or {}
    metadata = metadata or {}

    diff_data = AuditDiffService.diff_dicts(before_data, after_data)

    return ImportAuditLog.objects.create(
        branch=branch,
        import_batch=import_batch,
        job=job,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else "",
        before_data=before_data,
        after_data=after_data,
        diff_data=diff_data,
        message=message,
        metadata=metadata,
    )