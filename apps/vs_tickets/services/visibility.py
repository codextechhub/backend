from __future__ import annotations

from django.db.models import Q

from vs_rbac.permissions import user_has_rbac_permission

from ..constants import SUPPORT_USER_TYPES, TicketPermission
from ..models import Ticket


def is_support_user(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "user_type", "") in SUPPORT_USER_TYPES
    )


def has_ticket_permission(user, permission_key: str, school=None) -> bool:
    if is_support_user(user):
        return True
    return user_has_rbac_permission(user, permission_key, school=school)


def visible_tickets_qs(user):
    qs = Ticket.all_objects.select_related("requester", "assignee", "school", "branch")

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    if is_support_user(user):
        return qs

    # Requesters and assignees always see their own tickets; school-wide
    # visibility requires the seeded view grant on the user's school.
    visibility = Q(requester=user) | Q(assignee=user)
    school_id = getattr(user, "school_id", None)
    if school_id and has_ticket_permission(user, TicketPermission.VIEW, school=user.school):
        visibility |= Q(school_id=school_id)
    return qs.filter(visibility)


def can_view_ticket(user, ticket: Ticket) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_support_user(user):
        return True
    if ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return bool(
        ticket.school_id
        and ticket.school_id == getattr(user, "school_id", None)
        and has_ticket_permission(user, TicketPermission.VIEW, school=ticket.school)
    )


def can_manage_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    if ticket.assignee_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.MANAGE, school=ticket.school)


def can_update_ticket_fields(user, ticket: Ticket) -> bool:
    if can_manage_ticket(user, ticket):
        return True
    if ticket.requester_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.UPDATE, school=ticket.school)


def can_assign_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    return has_ticket_permission(user, TicketPermission.ASSIGN, school=ticket.school)


def can_comment_on_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    # Participants can always reply on their own thread.
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.COMMENT, school=ticket.school)


def can_attach_to_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.ATTACH, school=ticket.school)


def can_add_internal_note(user, ticket: Ticket) -> bool:
    if is_support_user(user) or ticket.assignee_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, school=ticket.school)


def can_view_internal_notes(user, ticket: Ticket) -> bool:
    return can_add_internal_note(user, ticket)


def sees_internal_notes_by_default(user) -> bool:
    """Ticket-independent variant used for list annotations, where per-ticket
    assignee checks don't apply (only support staff can be assignees)."""
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, school=getattr(user, "school", None))
