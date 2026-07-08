# =============================================================================
# vs_notifications / notify.py
#
# Public API for sending notifications from other apps.
#
# Usage:
#
#   from vs_notifications.notify import send_notification, UnregisteredRecipient
#
#   send_notification(
#       event_key="billing.invoice_issued",
#       context={
#           "customer_name":  customer.name,
#           "invoice_number": invoice.number,
#           "due_date":       invoice.due_date.strftime("%d %b %Y"),
#           "school_name":    school.name,
#       },
#       recipients=[guardian_user],
#       school=school,
#   )
#
# For inviting users who have no account yet, pass unregistered_recipients:
#
#   send_notification(
#       event_key="user.invited",
#       context={"invitation_url": url, "school_name": school.name},
#       recipients=[],
#       unregistered_recipients=[
#           UnregisteredRecipient(email="new@staff.com", name="Jane Doe"),
#       ],
#       metadata={"activation_key": key},   # internal-only correlation data
#   )
#
# `school` is OPTIONAL — notifications are recipient-centric. Pass it to scope
# history and pick up a school's settings overrides; omit it for school-less
# recipients (CX staff, invitees).
#
# All valid event_key values are listed in vs_notifications/constants.py
# under EVENT_TYPE_REGISTRY.
# =============================================================================

from typing import Optional

from .services.dispatch import NotificationService
from .services.dispatch import UnregisteredRecipient  # re-export for callers

__all__ = ["send_notification", "UnregisteredRecipient"]


def send_notification(
    event_key: str,
    context: dict,
    recipients: list,
    school=None,
    suppress: bool = False,
    unregistered_recipients: Optional[list[UnregisteredRecipient]] = None,
    metadata: Optional[dict] = None,
) -> list[str]:
    """
    Send a notification to one or more recipients for the given event.

    Args:
        event_key:               Dot-notation event key, e.g. "billing.invoice_issued".
                                 Must match an active entry in EVENT_TYPE_REGISTRY.
        context:                 Template variables dict. Keys required per event type
                                 are documented in constants.py EVENT_TYPE_REGISTRY.
        recipients:              List of User instances to notify.
        school:                  Optional School instance. Stored on each record for
                                 filtering/history and used to resolve school-specific
                                 settings overrides. Defaults to None (platform scope).
        suppress:                Pass True to skip dispatch entirely — useful when
                                 bulk-creating records where notifications would be noise.
        unregistered_recipients: List of UnregisteredRecipient(email, name) for
                                 recipients who have no User account yet (e.g. user.invited).
        metadata:                Optional internal-only dict stored on every created
                                 record (e.g. activation_key). Never serialized out.

    Returns:
        List of created Notification UUIDs as strings.
        Empty list if suppress=True or all channels are disabled.

    Raises:
        UnknownEventTypeError: If event_key is not a known active event type.
    """
    return NotificationService.send(
        event_key=event_key,
        context=context,
        recipients=recipients,
        school=school,
        suppress=suppress,
        unregistered_recipients=unregistered_recipients,
        metadata=metadata,
    )
