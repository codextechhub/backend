from __future__ import annotations

from rest_framework.permissions import BasePermission, SAFE_METHODS

class IsVisionStaff(BasePermission):
    """
    Simple gate:
    - allow only Django users with is_staff=True
    (Later you can swap this to a richer RBAC system.)
    """
    message = "Vision Admin Console access is restricted to staff users."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)
    
class StaffReadOnlyOrSuperuserWrite(BasePermission):
    """
    Staff can read.
    Only superusers can write.
    Useful for high-risk endpoints like provisioning/import logs if you want.
    """
    message = "Write access requires superuser."
    
    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(user.is_superuser)