from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from vs_user.models import User

from ..constants import (
    CommentVisibility,
    TicketAuditAction,
    TicketSource,
    TicketStatus,
    VALID_STATUS_TRANSITIONS,
)
from ..models import Ticket, TicketAttachment, TicketComment
from . import notifications as notify_svc
from .audit import record_ticket_audit, snapshot_ticket
from .visibility import (
    can_add_internal_note,
    can_assign_ticket,
    can_attach_to_ticket,
    can_comment_on_ticket,
    can_manage_ticket,
    can_update_ticket_fields,
    is_support_user,
)


# Default customer-created tickets to the actor's branch when available.
def _branch_for_actor(actor):
    return getattr(actor, "branch", None)


# Create the ticket, audit the opening state, and notify support triage.
def create_ticket(*, actor, title, description, category, priority, branch=None) -> Ticket:
    branch = branch if branch is not None else _branch_for_actor(actor)
    # Source distinguishes support-entered issues from customer/self-service tickets.
    source = TicketSource.INTERNAL if is_support_user(actor) else TicketSource.CUSTOMER

    with transaction.atomic():
        ticket = Ticket.objects.create(
            title=title,
            description=description,
            category=category,
            priority=priority,
            requester=actor,
            tenant=actor.tenant,
            branch=branch,
            source=source,
        )
        record_ticket_audit(
            ticket=ticket,
            action=TicketAuditAction.CREATED,
            actor=actor,
            summary=f"{actor.full_name} created ticket {ticket.ticket_number}.",
            after_data=snapshot_ticket(ticket),
        )
        notify_svc.notify_created(ticket, actor=actor)
        return ticket


# Update editable ticket fields while preserving before/after audit state.
def update_ticket(ticket: Ticket, *, actor, **updates) -> Ticket:
    if not can_update_ticket_fields(actor, ticket):
        raise PermissionDenied("You cannot update this ticket.")

    allowed = {"title", "description", "category", "priority"}
    # Drop service-layer attempts to mutate ownership or lifecycle through field updates.
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        return ticket

    with transaction.atomic():
        before = snapshot_ticket(ticket)
        for field, value in updates.items():
            setattr(ticket, field, value)
        ticket.full_clean()
        ticket.save(update_fields=[*updates.keys(), "updated_at"])
        after = snapshot_ticket(ticket)
        record_ticket_audit(
            ticket=ticket,
            action=TicketAuditAction.UPDATED,
            actor=actor,
            summary=f"{actor.full_name} updated ticket {ticket.ticket_number}.",
            before_data=before,
            after_data=after,
        )
        return ticket


# Assign or clear support ownership and synchronize the open/assigned status.
def assign_ticket(ticket: Ticket, *, actor, assignee: User | None) -> Ticket:
    if not can_assign_ticket(actor, ticket):
        raise PermissionDenied("You cannot assign this ticket.")
    if assignee is not None and not is_support_user(assignee):
        # Assignees must be support-capable; customers cannot become ticket owners.
        raise ValidationError({
            "assignee_id": ["Tickets can only be assigned to active staff who can manage tickets."],
        })

    with transaction.atomic():
        before = snapshot_ticket(ticket)
        ticket.assignee = assignee
        if assignee and ticket.status == TicketStatus.OPEN:
            # First assignment moves a new ticket into the assigned queue.
            ticket.status = TicketStatus.ASSIGNED
        elif assignee is None and ticket.status == TicketStatus.ASSIGNED:
            # Clearing the owner reopens the ticket for triage.
            ticket.status = TicketStatus.OPEN
        ticket.full_clean()
        ticket.save(update_fields=["assignee", "status", "updated_at"])
        record_ticket_audit(
            ticket=ticket,
            action=TicketAuditAction.ASSIGNED,
            actor=actor,
            summary=f"{actor.full_name} assigned ticket {ticket.ticket_number}.",
            before_data=before,
            after_data=snapshot_ticket(ticket),
            metadata={"assignee_id": assignee.pk if assignee else None},
        )
        if assignee:
            notify_svc.notify_assigned(ticket, actor=actor)
        return ticket


# Move a ticket through the allowed lifecycle graph and stamp terminal dates.
def transition_ticket(ticket: Ticket, *, actor, status: str) -> Ticket:
    if not can_manage_ticket(actor, ticket):
        raise PermissionDenied("You cannot change this ticket's status.")
    if status == ticket.status:
        # Repeating the current status is idempotent and should not create audit noise.
        return ticket
    if status not in VALID_STATUS_TRANSITIONS.get(ticket.status, set()):
        raise ValidationError(f"Cannot move ticket from {ticket.status} to {status}.")

    with transaction.atomic():
        before = snapshot_ticket(ticket)
        old_status = ticket.status
        ticket.status = status
        now = timezone.now()
        if status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
        elif status == TicketStatus.CLOSED:
            ticket.closed_at = now
        elif status == TicketStatus.IN_PROGRESS:
            if old_status == TicketStatus.CLOSED:
                # Reopening from closed clears the closure timestamp.
                ticket.closed_at = None
            if old_status == TicketStatus.RESOLVED:
                # Moving work back in progress clears resolution state.
                ticket.resolved_at = None
        ticket.full_clean()
        ticket.save(update_fields=["status", "resolved_at", "closed_at", "updated_at"])
        record_ticket_audit(
            ticket=ticket,
            action=TicketAuditAction.STATUS_CHANGED,
            actor=actor,
            summary=f"{actor.full_name} moved ticket {ticket.ticket_number} from {old_status} to {status}.",
            before_data=before,
            after_data=snapshot_ticket(ticket),
            metadata={"old_status": old_status, "new_status": status},
        )
        notify_svc.notify_status_changed(ticket, old_status=old_status, actor=actor)
        return ticket


# Add a public reply or internal note and notify the right audience.
def add_comment(ticket: Ticket, *, actor, body: str, visibility: str) -> TicketComment:
    if not can_comment_on_ticket(actor, ticket):
        raise PermissionDenied("You cannot comment on this ticket.")
    if visibility == CommentVisibility.INTERNAL and not can_add_internal_note(actor, ticket):
        raise PermissionDenied("You cannot add internal notes.")

    with transaction.atomic():
        comment = TicketComment.objects.create(
            ticket=ticket,
            author=actor,
            body=body,
            visibility=visibility,
        )
        # Internal notes use a distinct audit action so they are easy to filter.
        action = (
            TicketAuditAction.INTERNAL_NOTE_ADDED
            if visibility == CommentVisibility.INTERNAL
            else TicketAuditAction.COMMENTED
        )
        record_ticket_audit(
            ticket=ticket,
            action=action,
            actor=actor,
            summary=f"{actor.full_name} added a comment to ticket {ticket.ticket_number}.",
            metadata={"comment_id": comment.pk, "visibility": visibility},
        )
        notify_svc.notify_commented(comment, actor=actor)
        return comment


# Attach a file to the ticket or to one of its comments.
def add_attachment(ticket: Ticket, *, actor, file_obj, comment: TicketComment | None = None) -> TicketAttachment:
    if not can_attach_to_ticket(actor, ticket):
        raise PermissionDenied("You cannot attach files to this ticket.")
    if comment is not None and comment.ticket_id != ticket.pk:
        # Prevent a foreign comment id from linking files across ticket threads.
        raise ValidationError("Comment does not belong to this ticket.")

    with transaction.atomic():
        attachment = TicketAttachment.objects.create(
            ticket=ticket,
            comment=comment,
            uploaded_by=actor,
            file=file_obj,
            original_filename=getattr(file_obj, "name", ""),
            content_type=getattr(file_obj, "content_type", "") or "",
            size=getattr(file_obj, "size", 0) or 0,
        )
        record_ticket_audit(
            ticket=ticket,
            action=TicketAuditAction.ATTACHMENT_ADDED,
            actor=actor,
            summary=f"{actor.full_name} attached {attachment.original_filename} to ticket {ticket.ticket_number}.",
            metadata={"attachment_id": attachment.pk, "filename": attachment.original_filename},
        )
        notify_svc.notify_attachment_added(attachment, actor=actor)
        return attachment
