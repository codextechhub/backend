from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsVisionStaff(BasePermission):
    """
    Vision staff accounts are institution=NULL and user_type=VISION_STAFF.
    Adjust if your Vision staff rules differ.
    """
    def has_permission(self, request, view):
        u = getattr(request, "user", None)
        return bool(u and u.is_authenticated and getattr(u, "user_type", None) == "VISION_STAFF")


class IsInstitutionAdminOrVisionStaff(BasePermission):
    """
    Allows Vision staff OR institution admins.
    """
    def has_permission(self, request, view):
        u = getattr(request, "user", None)
        if not (u and u.is_authenticated):
            return False
        return getattr(u, "user_type", None) in ("VISION_STAFF", "INSTITUTION_ADMIN")


class IsSelfOrVisionStaff(BasePermission):
    """
    Object-level: user can access their own record, or Vision staff can access any.
    """
    def has_object_permission(self, request, view, obj):
        u = request.user
        if getattr(u, "user_type", None) == "VISION_STAFF":
            return True
        return obj.pk == u.pk


class IsReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
