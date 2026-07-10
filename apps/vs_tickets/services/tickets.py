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


def _school_for_actor(actor):
    return getattr(actor, "school", None)


def _branch_for_actor(actor):
    return getattr(actor, "branch", None)


def create_ticket(*, actor, title, description, category, priority, school=None, branch=None) -> Ticket:
    school = school if school is not None else _school_for_actor(actor)
    branch = branch if branch is not None else _branch_for_actor(actor)
    source = TicketSource.INTERNAL if is_support_user(actor) else TicketSource.CUSTOMER

    with transaction.atomic():
        ticket = Ticket.objects.create(
            title=title,
            description=description,
            category=category,
            priority=priority,
            requester=actor,
            school=school,
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


def update_ticket(ticket: Ticket, *, actor, **updates) -> Ticket:
    if not can_update_ticket_fields(actor, ticket):
        raise PermissionDenied("You cannot update this ticket.")

    allowed = {"title", "description", "category", "priority"}
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


def assign_ticket(ticket: Ticket, *, actor, assignee: User | None) -> Ticket:
    if not can_assign_ticket(actor, ticket):
        raise PermissionDenied("You cannot assign this ticket.")
    if assignee is not None and not is_support_user(assignee):
        raise ValidationError("Tickets can only be assigned to support staff.")

    with transaction.atomic():
        before = snapshot_ticket(ticket)
        ticket.assignee = assignee
        if assignee and ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.ASSIGNED
        elif assignee is None and ticket.status == TicketStatus.ASSIGNED:
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


def transition_ticket(ticket: Ticket, *, actor, status: str) -> Ticket:
    if not can_manage_ticket(actor, ticket):
        raise PermissionDenied("You cannot change this ticket's status.")
    if status == ticket.status:
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
                ticket.closed_at = None
            if old_status == TicketStatus.RESOLVED:
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


def add_attachment(ticket: Ticket, *, actor, file_obj, comment: TicketComment | None = None) -> TicketAttachment:
    if not can_attach_to_ticket(actor, ticket):
        raise PermissionDenied("You cannot attach files to this ticket.")
    if comment is not None and comment.ticket_id != ticket.pk:
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
