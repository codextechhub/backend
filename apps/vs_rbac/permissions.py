from __future__ import annotations

from django.db.models import Q
from rest_framework.permissions import BasePermission, SAFE_METHODS


def _get_user(obj, request):
    return getattr(request, "user", None)


def user_has_rbac_permission(user, permission_key, school=None):
    """
    Check whether *user* holds *permission_key* through any active role.

    For school-scoped users the check is limited to roles in *school*.
    For Vision staff the check runs against platform roles.

    Returns True if any active assignment grants the permission.
    """
    if not user or not user.is_authenticated:
        return False

    from .models import (
        RolePermission,
        UserRoleAssignment,
        PlatformRolePermission,
        PlatformUserRoleAssignment,
    )

    user_type = getattr(user, "user_type", "")

    # Vision staff: check platform roles
    if user_type == "VISION_STAFF":
        return PlatformRolePermission.objects.filter(
            role__user_assignments__user=user,
            role__user_assignments__assignment_status="ACTIVE",
            permission_id=permission_key,
            granted=True,
        ).exists()

    # School-scoped users: check school roles
    filters = Q(
        role__user_assignments__user=user,
        role__user_assignments__assignment_status="ACTIVE",
        permission_id=permission_key,
        granted=True,
    )
    if school is not None:
        filters &= Q(role__user_assignments__school=school)

    return RolePermission.objects.filter(filters).exists()


class IsAuthenticatedAndActive(BasePermission):
    """
    Minimal guardrail:
    - user must be authenticated
    - if your UserAccount has 'status', block locked/suspended
    """

    def has_permission(self, request, view):
        u = _get_user(None, request)
        if not u or not u.is_authenticated:
            return False

        # Optional (only if your user model has a 'status' field)
        status = getattr(u, "status", None)
        if status in {"SUSPENDED", "LOCKED", "DELETED"}:
            return False

        return True


class IsVisionStaff(BasePermission):
    """
    Vision staff can manage global permission registry + approve/deny requests.
    Assumes your user model has user_type and includes VISION_STAFF (Module 3).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(u, "user_type", "") == "VISION_STAFF"


class IsSchoolAdmin(BasePermission):
    """
    School admin can manage roles within their school.
    This is a simplified check: we treat user_type == SCHOOL_ADMIN as admin.
    If you want "anyone with permission", wire it to your RBAC evaluator later.
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(u, "user_type", "") == "SCHOOL_ADMIN"


class HasRBACPermission(BasePermission):
    """
    DRF permission that checks the user's RBAC roles for a specific key.

    Usage on a view::

        class InvoiceApproveView(APIView):
            permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
            rbac_permission = "finance.invoice.approve"

    You can also pass multiple keys (any-of)::

            rbac_permission = ["finance.invoice.approve", "finance.invoice.admin"]

    The school context is read from ``request.school`` (set by
    ``TenantContextMiddleware``).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False

        required = getattr(view, "rbac_permission", None)
        if required is None:
            return True  # no permission declared → pass through

        school = getattr(request, "school", None)

        if isinstance(required, str):
            required = [required]

        return any(
            user_has_rbac_permission(u, perm_key, school=school)
            for perm_key in required
        )


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
