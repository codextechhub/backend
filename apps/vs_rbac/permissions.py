from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS
from .evaluator import _group_permission_keys, has_permission, has_all_permissions


# Read the DRF request user through one helper so permission classes stay consistent.
def _get_user(obj, request):
    return getattr(request, "user", None)


# Check the platform super-admin assignment used for privileged RBAC bypasses.
def is_vision_super_admin(user):
    """Return True if *user* currently holds an active xvs_super_admin role.

    Memoised on the user instance — user objects are re-fetched on every
    request, so this saves one EXISTS query per permission check within a
    request without ever serving stale data across requests.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    cached = getattr(user, "_is_xvs_super_admin", None)
    if cached is not None:
        return cached  # Reuse the request-local assignment check.
    from .models import TenantUserRoleAssignment
    result = TenantUserRoleAssignment.objects.filter(
        user=user,
        tenant=getattr(user, "tenant", None),
        role__key="xvs_super_admin",
        role__tenant=getattr(user, "tenant", None),
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).exists()
    try:
        user._is_xvs_super_admin = result
    except AttributeError:
        pass
    return result


# Check a raw permission key against active school or platform role assignments.
def user_has_rbac_permission(user, permission_key, tenant=None, branch=None, school=None):
    """
    Check whether *user* holds *permission_key* through any active role.

    For school-scoped users the check is limited to roles in *school*.
    For Vision staff the check runs against platform roles.

    Returns True if any active assignment grants the permission.
    """
    if not user or not user.is_authenticated:
        return False

    return has_permission(
        user, permission_key, tenant=tenant, branch=branch, school=school,
    )


# Enforce login plus non-terminal account status before RBAC is evaluated.
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
        if status == "DEACTIVATED":
            raise PermissionDenied("This account has been deactivated. Contact your administrator.")

        return True


# Allow only Vision staff into platform-owned RBAC administration surfaces.
class IsVisionStaff(BasePermission):
    """
    Vision staff can manage global permission registry + approve/deny requests.
    Assumes your user model has user_type and includes VISION_STAFF (Module 3).
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(getattr(u, "tenant", None), "kind", None) == "PLATFORM"


# Allow only the active xvs_super_admin role holder into top-level controls.
class IsVisionSuperAdmin(BasePermission):
    """
    Grants access only to the active Vision Super Admin —
    the single user with an active xvs_super_admin TenantUserRoleAssignment.
    """

    def has_permission(self, request, view):
        return is_vision_super_admin(request.user)


# Allow school admins to manage school-local RBAC configuration.
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


# Evaluate the permission keys declared by a DRF view.
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
        if is_vision_super_admin(u):
            return True

        rbac_perms = getattr(view, "rbac_permission", None)
        rbac_group_perms = getattr(view, "rbac_group_permission", None)

        passed = True  # Both direct-key and group-key checks must remain satisfied.
        tenant = (
            getattr(request, "rbac_tenant", None)
            or getattr(request, "tenant", None)
            or getattr(u, "tenant", None)
        )
        branch = getattr(request, "branch", None)

        if rbac_perms is not None and rbac_perms != "":
            if isinstance(rbac_perms, list) and not rbac_perms:
                raise ImproperlyConfigured(
                    f"{view.__class__.__name__}.rbac_permission cannot be an empty list."
                )
            if isinstance(rbac_perms, str):
                rbac_perms = [rbac_perms]
            # Direct permissions are any-of so views can accept equivalent operation grants.
            if not any(
                has_permission(u, perm_key, tenant=tenant, branch=branch)
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
            
            perm_keys = _group_permission_keys(rbac_group_perms)  # Group checks require every key in the bundle.

            if not has_all_permissions(u, perm_keys, tenant=tenant, branch=branch):
                passed = False

        return passed



# Allow branch admins into branch-scoped management surfaces.
class IsBranchAdmin(BasePermission):
    """
    Grants access only to users with user_type == BRANCH_ADMIN.
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(u, "user_type", "") == "BRANCH_ADMIN"


# Permit safe HTTP methods on read-only endpoints.
class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
