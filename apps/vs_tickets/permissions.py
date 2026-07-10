from __future__ import annotations

from rest_framework.permissions import BasePermission

from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .services.visibility import is_support_user


class HasTicketRBACPermission(BasePermission):
    """``HasRBACPermission`` with the support-desk bypass.

    CX staff run the ticket desk, so they pass every ticket key check without
    needing per-key platform grants. Everyone else is checked against the
    view's ``rbac_permission`` key; views/actions that declare no key (ticket
    creation, own-ticket reads) fall through to queryset/object scoping.
    """

    def has_permission(self, request, view):
        if is_support_user(request.user):
            return True
        return HasRBACPermission().has_permission(request, view)


TICKET_PERMISSIONS = [IsAuthenticatedAndActive & HasTicketRBACPermission]
