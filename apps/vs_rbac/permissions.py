from __future__ import annotations

from rest_framework.permissions import BasePermission, SAFE_METHODS


def _get_user(obj, request):
    return getattr(request, "user", None)


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


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS