"""Recording what leaders do with other people's conversations.

An event names who did what to which conversation, with the conversation's
display name as its label. It never carries a message body: the messages are
the record, and the trail says only that somebody read, exported or changed
them.
"""
from __future__ import annotations

from vs_audit.models import AuditModuleKey
from vs_audit.services import emit_audit_event


def record(conversation, action, actor, summary, *, label: str = "", metadata: dict | None = None):
    """Write one audit event about *conversation*."""
    emit_audit_event(
        module_key=AuditModuleKey.MESSAGES,
        action_type=action,
        entity_type="conversation",
        entity_id=str(conversation.pk),
        entity_label=label or conversation.title or conversation.kind,
        tenant=conversation.tenant,
        actor_user=actor,
        summary=summary,
        metadata={"kind": conversation.kind, **(metadata or {})},
    )
