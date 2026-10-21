"""Where a parent's push opens in the parent app, and which child it is about.

Registered with the push layer (:func:`vs_notifications.services.push.register_destination`)
when the app loads. It recognises the three notifications a parent is pushed:

- a notice reaching their children opens that notice (``/parent/notices/<id>``);
  a notice can reach several of them, so it names no child;
- a released result (``gradebook.report_card_ready`` to the primary guardian,
  ``gradebook.result_released_parent`` to the others where the school tells
  every parent) opens Results with that child showing
  (``/parent/results?child=<id>``);
- a confirmed payment opens Fees with the child it paid for, and a new bill
  sent to the primary guardian opens Fees with the child it is for
  (``/parent/fees?child=<id>``).

A child is named only when the recipient is that child's parent at the school
and the child is one the parent app shows (``PORTAL_STATUSES``), so a link
never opens on another family's child or on one the app would refuse. When it
cannot be, the result and payment pushes are left to the push layer's default:
a pupil paying for themselves is the same event, and their push opens the
student app as before.
"""
from __future__ import annotations

from vs_notifications.services.push import PushDestination


def destination(notification):
    """A :class:`PushDestination` for a parent's notification, or None to pass."""
    from schools.vs_gradebook.constants import EVENT_REPORT_CARD_READY, EVENT_RESULT_RELEASED_PARENT
    from schools.vs_notices.constants import EVENT_PUBLISHED_PARENT, PARENT_APP_PATH

    key = notification.event_type.key
    metadata = notification.metadata or {}
    if key == EVENT_PUBLISHED_PARENT:
        notice_id = metadata.get("notice_id")
        return PushDestination(
            url=f"{PARENT_APP_PATH}notices/{notice_id}" if notice_id else f"{PARENT_APP_PATH}notices",
        )
    if key in (EVENT_REPORT_CARD_READY, EVENT_RESULT_RELEASED_PARENT):
        child = _own_child(notification.recipient, metadata.get("student_id"))
        screen = "results"
    elif key == "billing.payment_received":
        child = _own_child(notification.recipient, _paid_for(metadata.get("payment_id")))
        screen = "fees"
    elif key == "billing.guardian_invoice_issued":
        child = _own_child(notification.recipient, metadata.get("student_id"))
        screen = "fees"
    else:
        return None
    if child is None:
        return None
    return PushDestination(url=f"{PARENT_APP_PATH}{screen}?child={child}", child=child)


def _paid_for(payment_id):
    """The pupil a payment was made on the account of, as the books name them, or None."""
    from vs_finance.models import Payment

    if payment_id in (None, ""):
        return None
    source = (
        Payment.objects.filter(pk=payment_id)
        .values_list("customer__source_id", flat=True).first()
    )
    return source


def _own_child(user, student_id):
    """*student_id* as an int when it is a child *user* is a parent of and the app shows."""
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import StudentGuardian

    if user is None:
        return None
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        return None
    linked = StudentGuardian.all_objects.filter(
        tenant=user.tenant_id, guardian__tenant=user.tenant_id, guardian__user=user,
        student_id=student_id, student__status__in=PORTAL_STATUSES,
    ).exists()
    return student_id if linked else None
