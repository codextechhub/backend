"""Public API for sending notifications from other apps.

Usage:

  from vs_config.display import format_date
  from vs_notifications.notify import send_notification, UnregisteredRecipient

  send_notification(
      event_key="billing.invoice_issued",
      context={
          "customer_name":  customer.name,
          "invoice_number": invoice.number,
          "due_date":       format_date(invoice.due_date, tenant),
          "school_name":    school.name,
      },
      recipients=[guardian_user],
      school=school,
  )

A template prints its context as given, so a date or a time reaches it already
written, through vs_config.display (format_date, format_datetime, format_time),
in the tenant's date format, on its clock and in its zone (the branch's, for
a thing that belongs to one). Never pass an ISO string or strftime output for
a person to read.

For inviting users who have no account yet, pass unregistered_recipients:

  send_notification(
      event_key="user.invited",
      context={"invitation_url": url, "school_name": school.name},
      recipients=[],
      unregistered_recipients=[
          UnregisteredRecipient(email="new@staff.com", name="Jane Doe"),
      ],
      metadata={"activation_key": key},   # internal-only correlation data
  )

`school` is OPTIONAL - notifications are recipient-centric. Pass it to scope
history and pick up a school's settings overrides; omit it for school-less
recipients (CX staff, invitees).

`branch` is OPTIONAL too: the vs_tenants.Branch the event is about, such as
the branch an invoice is filed under. For an event registered branch_scoped,
that branch's own settings win over the tenant's. Pass it wherever the branch
is at hand; an event becomes branch_scoped only once every sender does.

All valid event_key values are listed in vs_notifications/constants.py
under EVENT_TYPE_REGISTRY.
"""
from typing import Optional

from .services.dispatch import NotificationService
# Re-export the invite recipient type so callers do not import service internals.
from .services.dispatch import UnregisteredRecipient

__all__ = ["send_notification", "UnregisteredRecipient"]


# Public notification entrypoint for domain services in other apps.
def send_notification(
    event_key: str,
    context: dict,
    recipients: list,
    tenant=None,
    school=None,
    branch=None,
    suppress: bool = False,
    unregistered_recipients: Optional[list[UnregisteredRecipient]] = None,
    metadata: Optional[dict] = None,
    delivery_replacements: Optional[dict[str, str]] = None,
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
        branch:                  Optional vs_tenants.Branch the event is about. A
                                 branch_scoped event consults its settings first.
        suppress:                Pass True to skip dispatch entirely - useful when
                                 bulk-creating records where notifications would be noise.
        unregistered_recipients: List of UnregisteredRecipient(email, name) for
                                 recipients who have no User account yet (e.g. user.invited).
        metadata:                Optional internal-only dict stored on every created
                                 record (e.g. activation_key). Never serialized out.
        delivery_replacements:   Optional marker-to-value substitutions applied by
                                 the email task immediately before SMTP. Values are
                                 never stored on Notification rows. Use this for
                                 one-time credentials that must not enter history.

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
        tenant=tenant,
        school=school,
        branch=branch,
        suppress=suppress,
        unregistered_recipients=unregistered_recipients,
        metadata=metadata,
        delivery_replacements=delivery_replacements,
    )
