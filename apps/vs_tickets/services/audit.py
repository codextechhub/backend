from __future__ import annotations

import logging

from vs_audit.services import emit_audit_event

from ..models import Ticket, TicketAuditLog

logger = logging.getLogger("vs_tickets.audit")


# Write both the local ticket audit log and the platform audit stream event.
def record_ticket_audit(
    *,
    ticket: Ticket,
    action: str,
    actor=None,
    summary: str = "",
    before_data: dict | None = None,
    after_data: dict | None = None,
    metadata: dict | None = None,
):
    from vs_tenants.context import add_proxy_audit_metadata, resolve_audit_identity

    actor, effective_user, proxy_session = resolve_audit_identity(actor)
    # Preserve impersonation context so support actions remain attributable.
    metadata = add_proxy_audit_metadata(metadata, effective_user, proxy_session)
    # Local ticket audit is the authoritative per-ticket history shown in the UI.
    log = TicketAuditLog.objects.create(
        ticket=ticket,
        actor=actor,
        action=action,
        summary=summary,
        before_data=before_data or {},
        after_data=after_data or {},
        metadata=metadata or {},
    )

    # Mirror into the platform audit trail for cross-module investigations.
    emit_audit_event(
        module_key="SYSTEM",
        action_type="CUSTOM",
        entity_type="Ticket",
        entity_id=str(ticket.pk),
        entity_label=ticket.ticket_number,
        actor_user=actor,
        summary=summary or f"Ticket {ticket.ticket_number}: {action}",
        before_data=before_data or {},
        diff_data=after_data or {},
        metadata={"ticket_action": action, **metadata},
    )
    return log


# Capture the mutable ticket fields needed for before/after audit diffs.
def snapshot_ticket(ticket: Ticket) -> dict:
    return {
        "id": ticket.pk,
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "description": ticket.description,
        "category": ticket.category,
        "priority": ticket.priority,
        "status": ticket.status,
        "requester_id": ticket.requester_id,
        "assignee_id": ticket.assignee_id,
        "tenant_id": ticket.tenant_id,
        "branch_id": ticket.branch_id,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
    }
