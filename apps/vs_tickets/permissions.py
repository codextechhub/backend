from __future__ import annotations

from rest_framework.permissions import BasePermission

from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .services.visibility import is_support_user


# Apply ticket RBAC while preserving the support-desk bypass.
class HasTicketRBACPermission(BasePermission):
    """``HasRBACPermission`` with the support-desk bypass.

    CX staff run the ticket desk, so they pass every ticket key check without
    needing per-key platform grants. Everyone else is checked against the
    view's ``rbac_permission`` key; views/actions that declare no key (ticket
    creation, own-ticket reads) fall through to queryset/object scoping.
    """

    def has_permission(self, request, view):
        if is_support_user(request.user):
            # CX support users operate the central desk without per-ticket tenant grants.
            return True
        return HasRBACPermission().has_permission(request, view)


# Every ticket endpoint first requires an active authenticated account.
TICKET_PERMISSIONS = [IsAuthenticatedAndActive & HasTicketRBACPermission]
