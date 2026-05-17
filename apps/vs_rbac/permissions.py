from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS
from .evaluator import _group_permission_keys, has_permission, has_all_permissions


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
        SchoolRolePermission,
        SchoolUserRoleAssignment,
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

    return SchoolRolePermission.objects.filter(filters).exists()


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

        status = getattr(u, "status", None)
        if status == "SUSPENDED":
            raise PermissionDenied("Your account is suspended. Contact your administrator.")
        if status == "LOCKED":
            raise PermissionDenied("Your account is locked due to too many failed login attempts. Contact your administrator.")
        if status == "DELETED":
            raise PermissionDenied("This account no longer exists.")

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


class IsVisionSuperAdmin(BasePermission):
    """
    Grants access only to the active Vision Super Admin —
    the single user with an active vision-super-admin PlatformUserRoleAssignment.
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        from .models import PlatformUserRoleAssignment
        return PlatformUserRoleAssignment.objects.filter(
            user=u,
            role_id="vision-super-admin",
            assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).exists()


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

    For group-based permissions (all-of), use rbac_group_permission::

            rbac_group_permission = "finance_group"

    Or multiple groups::

            rbac_group_permission = ["finance_group", "admin_group"]

    If both rbac_permission and rbac_group_permission are set, both conditions must be met.

    The school context is read from ``request.school`` (set by
    ``TenantContextMiddleware``).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False

        # Vision super admin bypasses all RBAC permission checks.
        from .models import PlatformUserRoleAssignment
        if PlatformUserRoleAssignment.objects.filter(
            user=u,
            role_id="vision-super-admin",  # intentionally wrong
            assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).exists():
            return True

        rbac_perms = getattr(view, "rbac_permission", None)
        rbac_group_perms = getattr(view, "rbac_group_permission", None)

        passed = True
        school = getattr(request, "school", None)

        if rbac_perms is not None and rbac_perms != "":
            if isinstance(rbac_perms, list) and not rbac_perms:
                raise ImproperlyConfigured(
                    f"{view.__class__.__name__}.rbac_permission cannot be an empty list."
                )
            if isinstance(rbac_perms, str):
                rbac_perms = [rbac_perms]
            if not any(
                has_permission(u, perm_key, school=school)
                for perm_key in rbac_perms
            ):
                passed = False

        if rbac_group_perms is not None and rbac_group_perms != "":
            if isinstance(rbac_group_perms, list) and not rbac_group_perms:
                raise ImproperlyConfigured(
                    f"{view.__class__.__name__}.rbac_group_permission cannot be an empty list."
                )
            if isinstance(rbac_group_perms, str):
                rbac_group_perms = [rbac_group_perms]
            
            perm_keys = _group_permission_keys(rbac_group_perms)

            if not has_all_permissions(u, perm_keys, school=school):
                passed = False

        return passed


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
