from __future__ import annotations

from django.db.models import Q

from vs_rbac.permissions import user_has_rbac_permission

from ..constants import TicketPermission
from ..models import Ticket


def is_support_user(user) -> bool:
    """Cross-tenant support console access: PLATFORM-tenant staff holding the
    manage grant. The kind check is load-bearing — a school user granted
    tickets.ticket.manage manages tickets inside their own tenant only and
    must never inherit the cross-tenant span."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(getattr(user, "tenant", None), "kind", None) != "PLATFORM":
        return False
    return user_has_rbac_permission(user, TicketPermission.MANAGE, tenant=user.tenant)


def has_ticket_permission(user, permission_key: str, tenant=None) -> bool:
    return user_has_rbac_permission(user, permission_key, tenant=tenant or user.tenant)


def visible_tickets_qs(user):
    qs = Ticket.all_objects.select_related("requester", "assignee", "tenant", "branch")

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    # Platform support (tickets.ticket.manage on the platform tenant) works the
    # cross-tenant support console — the one deliberate span over all tenants.
    if is_support_user(user):
        return qs

    qs = qs.filter(tenant=user.tenant)

    # A view grant is deliberately not school-wide ticket access: ticket
    # conversations can contain personal or operationally sensitive details.
    # Participants see their own threads, while same-tenant ticket managers
    # are the only non-participants allowed into them.
    visibility = Q(requester=user) | Q(assignee=user)
    if has_ticket_permission(user, TicketPermission.MANAGE, tenant=user.tenant):
        visibility |= Q(tenant=user.tenant)
    return qs.filter(visibility)


def can_view_ticket(user, ticket: Ticket) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_support_user(user):
        return True
    if ticket.tenant_id != user.tenant_id:
        return False
    if ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.MANAGE, tenant=ticket.tenant)


def can_manage_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    if ticket.assignee_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.MANAGE, tenant=ticket.tenant)


def can_update_ticket_fields(user, ticket: Ticket) -> bool:
    if can_manage_ticket(user, ticket):
        return True
    if ticket.requester_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.UPDATE, tenant=ticket.tenant)


def can_assign_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    return has_ticket_permission(user, TicketPermission.ASSIGN, tenant=ticket.tenant)


def can_comment_on_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    # Participants can always reply on their own thread.
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.COMMENT, tenant=ticket.tenant)


def can_attach_to_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.ATTACH, tenant=ticket.tenant)


def can_add_internal_note(user, ticket: Ticket) -> bool:
    if is_support_user(user) or ticket.assignee_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, tenant=ticket.tenant)


def can_view_internal_notes(user, ticket: Ticket) -> bool:
    return can_add_internal_note(user, ticket)


def sees_internal_notes_by_default(user) -> bool:
    """Ticket-independent variant used for list annotations, where per-ticket
    assignee checks don't apply (only support staff can be assignees)."""
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, tenant=user.tenant)
