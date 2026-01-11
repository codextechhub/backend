from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsVisionStaff(BasePermission):
    """
    Minimal internal-staff gate.
    Replace/extend with your real RBAC (groups/roles/claims).
    """

    message = "You do not have permission to access institution management."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        # Common patterns:
        # - user.is_staff for internal users
        # - user.is_superuser for super admins
        return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


class IsVisionSuperAdmin(BasePermission):
    """
    Elevated gate for destructive actions (hard delete / reset / overrides).
    """

    message = "Super admin privileges are required for this action."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        return bool(getattr(user, "is_superuser", False))


class ReadOnlyOrVisionStaff(IsVisionStaff):
    """
    Allows read-only access (GET/HEAD/OPTIONS) to authenticated users,
    but restricts writes to Vision Staff.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return super().has_permission(request, view)
