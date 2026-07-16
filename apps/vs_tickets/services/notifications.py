from __future__ import annotations

import logging

from django.db import transaction

from vs_notifications.services.dispatch import NotificationService
from vs_user.models import User

from ..constants import CommentVisibility, TicketPermission, TicketStatus

logger = logging.getLogger("vs_tickets.notifications")

# Holding either triage key marks a CX user as working the support queue.
TRIAGE_PERMISSION_KEYS = (TicketPermission.MANAGE, TicketPermission.ASSIGN)


# Deduplicate notification recipients and suppress echoing events back to the actor.
def _unique_recipients(users, *, exclude=None):
    exclude_id = getattr(exclude, "pk", None)
    seen = set()
    out = []
    for user in users:
        if not user or user.pk == exclude_id or user.pk in seen:
            continue
        seen.add(user.pk)
        out.append(user)
    return out


# Resolve the active platform users who should see new unassigned ticket activity.
def support_recipients():
    """Active platform-tenant users who hold a ticket triage key through an active
    platform role — not every platform user. Mirrors the platform-role branch of
    vs_rbac.permissions.user_has_rbac_permission."""
    return list(
        User.objects.filter(
            tenant__kind="PLATFORM",
            status=User.Status.ACTIVE,
            tenant_role_assignments__assignment_status="ACTIVE",
            tenant_role_assignments__role__role_permissions__permission_id__in=TRIAGE_PERMISSION_KEYS,
            tenant_role_assignments__role__role_permissions__granted=True,
        ).distinct()
    )


# Build the template context shared by ticket notification events.
def context_for(ticket, **extra):
    assignee_name = ticket.assignee.full_name if ticket.assignee_id else ""
    return {
        "ticket_number": ticket.ticket_number,
        "ticket_title": ticket.title,
        "ticket_status": ticket.status,
        "ticket_priority": ticket.priority,
        "ticket_category": ticket.category,
        "requester_name": ticket.requester.full_name,
        "assignee_name": assignee_name,
        **extra,
    }


# Queue a ticket notification after the surrounding transaction commits.
def dispatch_ticket_event(event_key: str, *, ticket, recipients, actor=None, context=None):
    recipients = _unique_recipients(recipients, exclude=actor)
    if not recipients:
        # No recipients is a valid no-op for unassigned or actor-only events.
        return []

    def _send():
        try:
            NotificationService.send(
                event_key=event_key,
                context=context or context_for(ticket),
                recipients=recipients,
                tenant=ticket.tenant,
                metadata={"ticket_id": ticket.pk, "ticket_number": ticket.ticket_number},
            )
        except Exception as exc:
            # Notification failure must not roll back ticket state or audit history.
            logger.warning("Ticket notification failed for %s: %s", event_key, exc)

    transaction.on_commit(_send)
    return recipients


# Notify the support queue when a new ticket needs triage.
def notify_created(ticket, actor=None):
    return dispatch_ticket_event(
        "ticket.created",
        ticket=ticket,
        actor=actor,
        recipients=support_recipients(),
        context=context_for(ticket, actor_name=getattr(actor, "full_name", "")),
    )


# Notify the assigned support user when ownership changes.
def notify_assigned(ticket, actor=None):
    return dispatch_ticket_event(
        "ticket.assigned",
        ticket=ticket,
        actor=actor,
        recipients=[ticket.assignee],
        context=context_for(ticket, actor_name=getattr(actor, "full_name", "")),
    )


# Notify participants with event names that match the lifecycle outcome.
def notify_status_changed(ticket, *, old_status, actor=None):
    event_key = {
        TicketStatus.RESOLVED: "ticket.resolved",
        TicketStatus.CLOSED: "ticket.closed",
    }.get(ticket.status)
    if event_key is None and old_status == TicketStatus.CLOSED:
        # Moving out of CLOSED is a reopen even when the target state is in progress.
        event_key = "ticket.reopened"
    if event_key is None:
        event_key = "ticket.status_changed"

    return dispatch_ticket_event(
        event_key,
        ticket=ticket,
        actor=actor,
        recipients=[ticket.requester, ticket.assignee],
        context=context_for(
            ticket,
            actor_name=getattr(actor, "full_name", ""),
            old_status=old_status,
            new_status=ticket.status,
        ),
    )


# Notify only support staff for internal notes; public comments notify both sides.
def notify_commented(comment, actor=None):
    if comment.visibility == CommentVisibility.INTERNAL:
        recipients = [comment.ticket.assignee]
    else:
        recipients = [comment.ticket.requester, comment.ticket.assignee]
    return dispatch_ticket_event(
        "ticket.commented",
        ticket=comment.ticket,
        actor=actor,
        recipients=recipients,
        context=context_for(
            comment.ticket,
            actor_name=getattr(actor, "full_name", ""),
            comment_body=comment.body,
            comment_visibility=comment.visibility,
        ),
    )


# Notify ticket participants when a file is attached.
def notify_attachment_added(attachment, actor=None):
    return dispatch_ticket_event(
        "ticket.attachment_added",
        ticket=attachment.ticket,
        actor=actor,
        recipients=[attachment.ticket.requester, attachment.ticket.assignee],
        context=context_for(
            attachment.ticket,
            actor_name=getattr(actor, "full_name", ""),
            attachment_name=attachment.original_filename,
        ),
    )
