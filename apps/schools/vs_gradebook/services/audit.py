"""The gradebook audit trail.

Every change to an assessment structure, a grading scale or the principal's
remark templates, and every submission, decision, publication and hold, is
recorded under the ``GRADEBOOK`` module, against the record it belongs to. The sentence is written for the person reading the trail,
so it names the programme, the structure or the class rather than ids; the ids
go in the metadata.

A change to a gradebook setting is recorded by ``vs_config`` itself
(``config.value.updated``) when the value is written, so it is not recorded a
second time here.
"""
from __future__ import annotations

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event


def record(
    *, tenant, user, branch, entity_type, entity_id, entity_label, summary,
    action_type=AuditActionType.UPDATE, **metadata,
) -> None:
    """Write one gradebook event. Never raises, as every audit write does not."""
    emit_audit_event(
        module_key=AuditModuleKey.GRADEBOOK,
        action_type=action_type,
        entity_type=entity_type, entity_id=str(entity_id), entity_label=entity_label,
        tenant=tenant, actor_user=user, branch=branch,
        summary=summary, metadata=metadata,
    )
