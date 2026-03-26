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


class IsReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
    

class IsVisionStaffOrSuperuser(BasePermission):
    message = "Only Vision staff or superusers can perform this action."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        return (
            getattr(user, "user_type", None) == "VISION_STAFF"
            or getattr(user, "is_superuser", False)
        )
        
from rest_framework.permissions import BasePermission


class IsSelfOrVisionStaff(BasePermission):
    """
    Object-level:
    - user can access their own record
    - Vision staff can access any record
    - superuser can access any record
    """
    def has_object_permission(self, request, view, obj):
        u = request.user

        if not u or not u.is_authenticated:
            return False

        if getattr(u, "is_superuser", False):
            return True

        if getattr(u, "user_type", None) == "VS_STAFF":
            return True

        return obj.pk == u.pk