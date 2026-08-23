from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from vs_rbac.permissions import user_has_rbac_permission
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.scoping import branch_q_for_user, visible_branch_ids
from vs_user.models import User

from ..constants import TicketPermission
from ..models import Ticket


# Decide whether a user can operate the cross-tenant support desk.
def is_support_user(user) -> bool:
    """Cross-tenant support console access: PLATFORM-tenant staff holding the
    manage grant. The kind check is load-bearing - a school user granted
    tickets.ticket.manage manages tickets inside their own tenant only and
    must never inherit the cross-tenant span."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(getattr(user, "tenant", None), "kind", None) != "PLATFORM":
        return False
    return user_has_rbac_permission(user, TicketPermission.MANAGE, tenant=user.tenant)


# Return active CX users who can be assigned support tickets.
def eligible_support_users_qs():
    """Active platform users whose effective RBAC grants ticket management."""
    # Match effective tenant-level roles without pulling every role into Python.
    from vs_rbac.evaluator import ANY_BRANCH, _assignment_branch_q

    active_roles = TenantUserRoleAssignment.objects.filter(
        user_id=OuterRef("pk"),
        tenant_id=OuterRef("tenant_id"),
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        role__status="ACTIVE",
        # Was a bare ``branch__isnull=True``. That is the same question
        # ``is_support_user`` asks one function above, and the two must not answer
        # differently: somebody the gate admits as a ticket manager has to appear
        # in the list of people a ticket can be assigned to. No platform tenant
        # has branches today, so this widens nothing now - it stops the two
        # drifting the moment one does.
    ).filter(_assignment_branch_q(ANY_BRANCH))
    grants_manage = active_roles.filter(
        Q(
            role__role_permissions__permission_id=TicketPermission.MANAGE,
            role__role_permissions__granted=True,
        )
        | Q(
            role__role_groups__group__group_permissions__permission_id=TicketPermission.MANAGE,
        )
    )
    # Explicit direct denies win over role/group grants for assignment eligibility.
    denies_manage = active_roles.filter(
        role__role_permissions__permission_id=TicketPermission.MANAGE,
        role__role_permissions__granted=False,
    )
    return (
        User.objects.filter(
            # One filter, not two. This used to say ``tenant__kind="PLATFORM"``
            # AND ``user_type=CX_STAFF`` - hedging against the two disagreeing,
            # which was a reasonable thing to fear while both existed.
            tenant__kind="PLATFORM",
            status=User.Status.ACTIVE,
            is_active=True,
        )
        .annotate(
            _grants_ticket_manage=Exists(grants_manage),
            _denies_ticket_manage=Exists(denies_manage),
        )
        .filter(_grants_ticket_manage=True, _denies_ticket_manage=False)
        .order_by("first_name", "last_name", "email")
    )


# Check a ticket permission inside the ticket tenant unless a caller supplies another scope.
def has_ticket_permission(user, permission_key: str, tenant=None) -> bool:
    return user_has_rbac_permission(user, permission_key, tenant=tenant or user.tenant)


# Build the ticket queryset a user may list or search.
def visible_tickets_qs(user):
    # ``__tenant`` on both users: TicketUserSerializer reports the tenant kind,
    # so without the join every row costs two extra queries.
    qs = Ticket.all_objects.select_related(
        "requester__tenant", "assignee__tenant", "tenant", "branch",
    )

    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    # Platform support (tickets.ticket.manage on the platform tenant) works the
    # cross-tenant support console - the one deliberate span over all tenants.
    if is_support_user(user):
        return qs

    qs = qs.filter(tenant=user.tenant)

    # A view grant is deliberately not school-wide ticket access: ticket
    # conversations can contain personal or operationally sensitive details.
    # Participants see their own threads, while same-tenant ticket managers
    # are the only non-participants allowed into them.
    visibility = Q(requester=user) | Q(assignee=user)
    if has_ticket_permission(user, TicketPermission.MANAGE, tenant=user.tenant):
        # Branch narrowing belongs to *this* arm and to nothing else. A manager
        # pinned to Ikeja manages Ikeja's tickets and the ones filed for the
        # school as a whole; she does not manage Lekki's.
        #
        # It must not be ANDed over the whole predicate. The participant arm
        # above is somebody's own thread, and a ticket carries the branch it was
        # filed for, not the branch of everyone on it: narrowing there would take
        # a ticket away from the person assigned to work it, which gets reported
        # as "the app lost my ticket" rather than as a permissions rule.
        #
        # ``include_shared=True``: a null branch here means the ticket concerns
        # the school rather than one site, and a branch manager must still see it.
        visibility |= (
            Q(tenant=user.tenant)
            & branch_q_for_user(user, include_shared=True)
        )
    return qs.filter(visibility)


# Object-level visibility guard used to return 404 for hidden tickets.
def can_view_ticket(user, ticket: Ticket) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_support_user(user):
        return True
    if ticket.tenant_id != user.tenant_id:
        # Non-support users never cross tenant boundaries.
        return False
    if ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        # A participant keeps their own thread whatever branch it carries; see
        # visible_tickets_qs for why the narrowing sits below this line, not above.
        return True
    if not has_ticket_permission(user, TicketPermission.MANAGE, tenant=ticket.tenant):
        return False
    # The object-level twin of the manager arm's narrowing. Without it the list
    # and the detail read disagree, and a manager could open by id exactly the
    # ticket her own list refused to show her.
    branch_ids = visible_branch_ids(user, user.tenant)
    return (
        branch_ids is None
        or ticket.branch_id is None  # Filed for the school as a whole: shared.
        or ticket.branch_id in branch_ids
    )


# Decide who can perform support-owner actions on a ticket.
def can_manage_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    if ticket.assignee_id == getattr(user, "pk", None):
        # Assignees can progress the ticket even without broader tenant management.
        return True
    return has_ticket_permission(user, TicketPermission.MANAGE, tenant=ticket.tenant)


# Decide who can edit mutable ticket fields.
def can_update_ticket_fields(user, ticket: Ticket) -> bool:
    # The requester owns the problem statement. Management and assignment
    # grants allow resolvers to progress the workflow, not rewrite what the
    # requester reported.
    return ticket.requester_id == getattr(user, "pk", None)


# Decide who can assign or unassign ticket ownership.
def can_assign_ticket(user, ticket: Ticket) -> bool:
    if is_support_user(user):
        return True
    return has_ticket_permission(user, TicketPermission.ASSIGN, tenant=ticket.tenant)


# Decide who can add public replies to a ticket thread.
def can_comment_on_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    # Participants can always reply on their own thread.
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.COMMENT, tenant=ticket.tenant)


# Decide who can attach files to a visible ticket.
def can_attach_to_ticket(user, ticket: Ticket) -> bool:
    if not can_view_ticket(user, ticket):
        return False
    if is_support_user(user) or ticket.requester_id == user.pk or ticket.assignee_id == user.pk:
        return True
    return has_ticket_permission(user, TicketPermission.ATTACH, tenant=ticket.tenant)


# Decide who can add support-only internal notes.
def can_add_internal_note(user, ticket: Ticket) -> bool:
    if is_support_user(user) or ticket.assignee_id == getattr(user, "pk", None):
        return True
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, tenant=ticket.tenant)


# Internal-note visibility mirrors the ability to create internal notes.
def can_view_internal_notes(user, ticket: Ticket) -> bool:
    return can_add_internal_note(user, ticket)


# List-query helper for whether internal note counts can be included.
def sees_internal_notes_by_default(user) -> bool:
    """Ticket-independent variant used for list annotations, where per-ticket
    assignee checks don't apply (only support staff can be assignees)."""
    return has_ticket_permission(user, TicketPermission.INTERNAL_NOTE, tenant=user.tenant)
