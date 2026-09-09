from __future__ import annotations

import logging

from django.db import transaction

from vs_notifications.services.dispatch import NotificationService
from vs_user.models import User

from ..constants import CommentVisibility, TicketPermission, TicketStatus
from . import subscriptions as subscription_svc
from .visibility import can_view_internal_notes, can_view_ticket

logger = logging.getLogger("vs_tickets.notifications")

#: Holding either key marks somebody as working a ticket queue. Inside a school
#: that resolves to ``manage`` alone, because ``assign`` is CodeX's own key: the
#: desk chooses which of its people owns a ticket, and a school's say is
#: escalation.
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


def _triage_recipients_in(tenant):
    """Active users of ``tenant`` who hold a ticket triage key through an active role.

    Mirrors the role branch of vs_rbac.permissions.user_has_rbac_permission
    rather than calling it per user, so a queue notification costs one query
    instead of one per member of staff.
    """
    return list(
        User.objects.filter(
            tenant=tenant,
            status=User.Status.ACTIVE,
            tenant_role_assignments__assignment_status="ACTIVE",
            tenant_role_assignments__role__role_permissions__permission_id__in=TRIAGE_PERMISSION_KEYS,
            tenant_role_assignments__role__role_permissions__granted=True,
        ).distinct()
    )


# Resolve the active platform users who should see new unassigned ticket activity.
def support_recipients():
    """Active platform-tenant users who hold a ticket triage key through an active
    platform role - not every platform user."""
    from vs_tenants.models import Tenant

    return list(
        User.objects.filter(
            tenant__kind=Tenant.Kind.PLATFORM,
            status=User.Status.ACTIVE,
            tenant_role_assignments__assignment_status="ACTIVE",
            tenant_role_assignments__role__role_permissions__permission_id__in=TRIAGE_PERMISSION_KEYS,
            tenant_role_assignments__role__role_permissions__granted=True,
        ).distinct()
    )


# Resolve whoever owns an unassigned ticket's queue right now.
def triage_recipients(ticket):
    """The people to tell about activity on a ticket nobody has picked up.

    Which queue that is depends on where the ticket sits. A school's ticket is
    the school's own until they escalate it, so it is their triage staff who
    need to know somebody raised it, not CodeX. After escalation, and for
    CodeX's own tickets, it is the platform desk.

    Getting this wrong is silent in the worst way. ``_eligible_recipients``
    drops anybody who cannot view the ticket, so paging the platform desk about
    an unescalated school ticket does not send a wrong email - it sends none at
    all, and the ticket sits unread with nobody aware it exists.
    """
    from vs_tenants.models import Tenant

    if ticket.escalated_at is not None:
        return support_recipients()
    if getattr(ticket.tenant, "kind", None) == Tenant.Kind.PLATFORM:
        return support_recipients()
    return _triage_recipients_in(ticket.tenant)


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


def _ticket_participants(ticket):
    return [
        ticket.requester,
        ticket.assignee,
        *subscription_svc.active_users(ticket),
    ]


def _eligible_recipients(ticket, users, *, exclude=None, internal=False, respect_mute=True):
    recipients = _unique_recipients(users, exclude=exclude)
    muted_ids = (
        subscription_svc.muted_user_ids(ticket, [user.pk for user in recipients])
        if respect_mute
        else set()
    )
    can_receive = can_view_internal_notes if internal else can_view_ticket
    return [
        user
        for user in recipients
        if user.pk not in muted_ids
        and user.status == User.Status.ACTIVE
        and user.is_active
        and can_receive(user, ticket)
    ]


def dispatch_ticket_event(
    event_key: str,
    *,
    ticket,
    recipients,
    actor=None,
    context=None,
    internal=False,
    respect_mute=True,
):
    # Queue a ticket notification after the surrounding transaction commits.
    recipients = _eligible_recipients(
        ticket,
        recipients,
        exclude=actor,
        internal=internal,
        respect_mute=respect_mute,
    )
    if not recipients:
        # No recipients is a valid no-op for unassigned or actor-only events.
        return []

    def _send():
        try:
            NotificationService.send(
                event_key=event_key,
                context=context or context_for(ticket),
                recipients=recipients,
                # What the message is about, not who owns the rows. Dispatch stores this
                # as origin_tenant and gives each record to its recipient's own tenant.
                tenant=ticket.tenant,
                metadata={"ticket_id": ticket.pk, "ticket_number": ticket.ticket_number},
            )
        except Exception as exc:
            # Notification failure must not roll back ticket state or audit history.
            logger.warning("Ticket notification failed for %s: %s", event_key, exc)

    transaction.on_commit(_send)
    return recipients


def notify_escalated(ticket, actor=None):
    """Tell the platform desk that a school has handed them a ticket.

    Without this, escalation is silent to the side that gains the work: the row
    joins the desk's list and nothing announces it. The next comment would
    eventually route there through ``triage_recipients``, but only if somebody
    happens to write one, and the school escalated precisely because they were
    waiting.

    Addressed to the desk alone. The school already knows - the reader who
    escalated did it, and the note posted alongside it is what tells the person
    who raised the ticket.

    The school's name is passed here rather than added to ``context_for``: this
    is the one ticket message that arrives from somewhere else, and every other
    one would pay for the lookup without using it.
    """
    return dispatch_ticket_event(
        "ticket.escalated",
        ticket=ticket,
        actor=actor,
        recipients=support_recipients(),
        context=context_for(
            ticket,
            actor_name=getattr(actor, "full_name", ""),
            school_name=getattr(ticket.tenant, "name", "") or "",
        ),
    )


# Notify the support queue when a new ticket needs triage.
def notify_created(ticket, actor=None):
    return dispatch_ticket_event(
        "ticket.created",
        ticket=ticket,
        actor=actor,
        recipients=triage_recipients(ticket),
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
        # Direct assignment creates responsibility even for an owner who has
        # muted the conversation.
        respect_mute=False,
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
        recipients=_ticket_participants(ticket),
        context=context_for(
            ticket,
            actor_name=getattr(actor, "full_name", ""),
            old_status=old_status,
            new_status=ticket.status,
        ),
    )


# Notify every eligible participant without exposing internal notes to requesters.
def notify_commented(comment, actor=None):
    recipients = _ticket_participants(comment.ticket)
    if comment.visibility != CommentVisibility.INTERNAL:
        # Until assignment the queue is the other side of the public
        # conversation; without it a requester's reply reaches nobody.
        if (
            comment.ticket.assignee_id is None
            and comment.author_id == comment.ticket.requester_id
        ):
            recipients.extend(triage_recipients(comment.ticket))
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
        internal=comment.visibility == CommentVisibility.INTERNAL,
    )


# Notify ticket participants when a file is attached.
def notify_attachment_added(attachment, actor=None):
    recipients = _ticket_participants(attachment.ticket)
    internal = bool(
        attachment.comment_id
        and attachment.comment.visibility == CommentVisibility.INTERNAL
    )
    if (
        attachment.ticket.assignee_id is None
        and attachment.uploaded_by_id == attachment.ticket.requester_id
    ):
        recipients.extend(triage_recipients(attachment.ticket))
    return dispatch_ticket_event(
        "ticket.attachment_added",
        ticket=attachment.ticket,
        actor=actor,
        recipients=recipients,
        context=context_for(
            attachment.ticket,
            actor_name=getattr(actor, "full_name", ""),
            attachment_name=attachment.original_filename,
        ),
        internal=internal,
    )
