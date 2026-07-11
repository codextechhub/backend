from __future__ import annotations

import logging

from django.db import transaction

from vs_notifications.services.dispatch import NotificationService
from vs_user.models import User

from ..constants import CommentVisibility, TicketPermission, TicketStatus

logger = logging.getLogger("vs_tickets.notifications")

# Holding either triage key marks a CX user as working the support queue.
TRIAGE_PERMISSION_KEYS = (TicketPermission.MANAGE, TicketPermission.ASSIGN)


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


def support_recipients():
    """Active CX staff who hold a ticket triage key through an active platform
    role — not every CX_STAFF user. Mirrors the platform-role branch of
    vs_rbac.permissions.user_has_rbac_permission."""
    return list(
        User.objects.filter(
            user_type=User.UserType.CX_STAFF,
            status=User.Status.ACTIVE,
            platform_role_assignments__assignment_status="ACTIVE",
            platform_role_assignments__role__role_permissions__permission_id__in=TRIAGE_PERMISSION_KEYS,
            platform_role_assignments__role__role_permissions__granted=True,
        ).distinct()
    )


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


def dispatch_ticket_event(event_key: str, *, ticket, recipients, actor=None, context=None):
    recipients = _unique_recipients(recipients, exclude=actor)
    if not recipients:
        return []

    def _send():
        try:
            NotificationService.send(
                event_key=event_key,
                context=context or context_for(ticket),
                recipients=recipients,
                school=ticket.school,
                metadata={"ticket_id": ticket.pk, "ticket_number": ticket.ticket_number},
            )
        except Exception as exc:
            logger.warning("Ticket notification failed for %s: %s", event_key, exc)

    transaction.on_commit(_send)
    return recipients


def notify_created(ticket, actor=None):
    return dispatch_ticket_event(
        "ticket.created",
        ticket=ticket,
        actor=actor,
        recipients=support_recipients(),
        context=context_for(ticket, actor_name=getattr(actor, "full_name", "")),
    )


def notify_assigned(ticket, actor=None):
    return dispatch_ticket_event(
        "ticket.assigned",
        ticket=ticket,
        actor=actor,
        recipients=[ticket.assignee],
        context=context_for(ticket, actor_name=getattr(actor, "full_name", "")),
    )


def notify_status_changed(ticket, *, old_status, actor=None):
    event_key = {
        TicketStatus.RESOLVED: "ticket.resolved",
        TicketStatus.CLOSED: "ticket.closed",
    }.get(ticket.status)
    if event_key is None and old_status == TicketStatus.CLOSED:
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
