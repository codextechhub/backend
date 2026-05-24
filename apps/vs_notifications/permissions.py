# =============================================================================
# vs_notifications / permissions.py
#
# DRF permission classes.  All permission checks are evaluated at the view
# level.  Queryset scoping (school isolation) is enforced separately in
# each ViewSet's get_queryset() method.
# =============================================================================

from rest_framework.permissions import BasePermission

from .constants import NotificationPermission


class IsVisionStaff(BasePermission):
    """
    Grants access to Vision Staff only.
    Used for template management and cross-school history queries.
    """
    message = "This action is restricted to Vision Staff."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "is_vision_staff", False)
        )


class HasTemplateConfigurePermission(BasePermission):
    """
    Checks the communication.notification_templates.configure RBAC key.
    Restricted to Vision Staff.
    """
    message = "You do not have permission to manage notification templates."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm(NotificationPermission.TEMPLATE_CONFIGURE)
        )


class HasAuditPermission(BasePermission):
    """
    Checks the communication.message_activity.audit RBAC key.
    Required for accessing the notification history log.
    """
    message = "You do not have permission to view the notification history log."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm(NotificationPermission.AUDIT_ACTIVITY)
        )


class HasEnforcePermissionsKey(BasePermission):
    """
    Checks the communication.communication_permissions.enforce RBAC key.
    Required for School Admins to view and update notification settings.
    """
    message = "You do not have permission to manage notification settings."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm(NotificationPermission.ENFORCE_PERMISSIONS)
        )


class IsNotificationRecipient(BasePermission):
    """
    Object-level permission.
    Ensures a user can only access their own Notification records.
    Applied on detail endpoints: GET /notifications/{id}/.
    """
    message = "You do not have permission to access this notification."

    def has_object_permission(self, request, view, obj):
        # Vision Staff can access any notification for support purposes
        if getattr(request.user, "is_vision_staff", False):
            return True
        return obj.recipient_id == request.user.id
