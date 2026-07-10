# =============================================================================
# vs_notifications / exceptions.py
#
# Typed domain exceptions.  All exceptions carry an error_code that maps
# directly to the NotificationErrorCode constants, making API error responses
# consistent and machine-readable.
# =============================================================================

from .constants import NotificationErrorCode


# Carry a machine-readable code alongside every notification domain failure.
class NotificationBaseException(Exception):
    """Base class for all vs_notifications exceptions."""
    error_code = "NOTIFICATION_ERROR"
    default_message = "A notification error occurred."

    def __init__(self, message=None, **kwargs):
        self.message = message or self.default_message
        self.extra = kwargs
        super().__init__(self.message)


# Fail dispatch when a caller references an inactive or unseeded event.
class UnknownEventTypeError(NotificationBaseException):
    """
    Raised when NotificationService.send() receives an event_key that does
    not match any active NotificationEventType.
    """
    error_code = NotificationErrorCode.UNKNOWN_EVENT_TYPE
    default_message = "The specified notification event type is unknown or inactive."


# Prevent accidental duplicate templates for the same event/channel pair.
class DuplicateTemplateError(NotificationBaseException):
    """
    Raised when attempting to create a NotificationTemplate for an
    (event_type, channel) pair that already has a template.
    """
    error_code = NotificationErrorCode.DUPLICATE_TEMPLATE
    default_message = (
        "A template for this event type and channel already exists. "
        "Update the existing template instead of creating a new one."
    )


# Surface template syntax failures before a broken template is saved.
class InvalidTemplateSyntaxError(NotificationBaseException):
    """
    Raised when a NotificationTemplate body or subject contains invalid
    Django template syntax.  Carries the line number if available.
    """
    error_code = NotificationErrorCode.INVALID_TEMPLATE_SYNTAX
    default_message = "The template contains invalid syntax."

    def __init__(self, message=None, field=None, **kwargs):
        super().__init__(message, **kwargs)
        # Store the editable field so the API can point admins to the bad template part.
        self.field = field


# Reject read-state operations on channels that never create in-app feed rows.
class ReadStateNotSupportedError(NotificationBaseException):
    """
    Raised when a mark-read request targets a notification on the EMAIL
    channel, which has no read state.
    """
    error_code = NotificationErrorCode.READ_STATE_NOT_SUPPORTED_FOR_CHANNEL
    default_message = (
        "Read state is not supported for email notifications. "
        "Only in-app notifications can be marked as read."
    )


# Enforce product policy that in-app notifications are always available.
class InAppAlwaysEnabledError(NotificationBaseException):
    """
    Raised when a School Admin attempts to disable the IN_APP channel
    for any notification event type.
    """
    error_code = NotificationErrorCode.IN_APP_ALWAYS_ENABLED
    default_message = (
        "The in-app channel cannot be disabled. "
        "Only the email channel can be toggled per event type."
    )


# Protect the global history log from unbounded Vision staff queries.
class FilterRequiredError(NotificationBaseException):
    """
    Raised when a Vision Staff user queries the notification history log
    without providing at least one required filter.
    """
    error_code = NotificationErrorCode.FILTER_REQUIRED
    default_message = (
        "At least one filter is required to query the notification history log. "
        "Provide one of: school_id, recipient_email, or a date range."
    )


# Hide cross-recipient or cross-school notification records.
class NotificationAccessDeniedError(NotificationBaseException):
    """
    Raised when a user attempts to access a notification record belonging
    to another user or another school.
    """
    error_code = NotificationErrorCode.ACCESS_DENIED
    default_message = "You do not have permission to access this notification."


# Represent dispatch-time rendering failures without treating the template as unsaved syntax.
class TemplateRenderError(NotificationBaseException):
    """
    Raised by the render service when template rendering fails at dispatch
    time (e.g. a context variable causes a runtime error).  This is distinct
    from InvalidTemplateSyntaxError which fires at save time.
    """
    error_code = "TEMPLATE_RENDER_ERROR"
    default_message = "Failed to render the notification template."
