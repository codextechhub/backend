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
        return getattr(u, "user_type", "") == "VS_STAFF"


class IsInstitutionAdmin(BasePermission):
    """
    Institution admin can manage roles within their institution.
    This is a simplified check: we treat user_type == INSTITUTION_ADMIN as admin.
    If you want "anyone with permission", wire it to your RBAC evaluator later.
    """

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return getattr(u, "user_type", "") == "INSTITUTION_ADMIN"


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


class HasRBACPermission(BasePermission):
    """
    DRF permission class that checks RBAC-assigned permissions.

    Usage:
        permission_classes = [IsAuthenticatedAndActive, HasRBACPermission("finance.invoice.approve")]

    For institution-scoped views, the institution is resolved from the URL kwarg
    "institution_id" (override via `institution_url_kwarg` on the view).
    """

    def __init__(self, *required_permissions: str):
        self.required_permissions = required_permissions

    def __call__(self):
        """Allow DRF to instantiate this class; return self since already configured."""
        return self

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        from .evaluator import has_all_permissions

        # Resolve institution from URL kwargs if available
        institution = None
        institution_kwarg = getattr(view, "institution_url_kwarg", "institution_id")
        institution_id = getattr(view, "kwargs", {}).get(institution_kwarg)

        if institution_id:
            from vs_institutions.models import Institution
            try:
                institution = Institution.objects.get(pk=institution_id)
            except Institution.DoesNotExist:
                return False

        return has_all_permissions(
            user,
            list(self.required_permissions),
            institution=institution,
        )