"""The attendance audit trail.

Every submit, change and correction decision is recorded under the
``ATTENDANCE`` module, against the register it belongs to. The sentence is
written for the person reading the trail, so it names the student, the marks
and the class rather than ids; the ids go in the metadata.
"""
from __future__ import annotations

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_config.display import format_date


def register_label(school_class, on_date, tenant, branch) -> str:
    """How the trail names a register: the class and the day."""
    return f"{school_class.name}, {format_date(on_date, tenant, branch=branch)}"


def record(
    *, tenant, user, branch, entity_type, entity_id, entity_label, summary,
    action_type=AuditActionType.UPDATE, **metadata,
) -> None:
    """Write one attendance event. Never raises, as every audit write does not."""
    emit_audit_event(
        module_key=AuditModuleKey.ATTENDANCE,
        action_type=action_type,
        entity_type=entity_type, entity_id=str(entity_id), entity_label=entity_label,
        tenant=tenant, actor_user=user, branch=branch,
        summary=summary, metadata=metadata,
    )
