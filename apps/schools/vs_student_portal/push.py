"""Where a pupil's payment push opens in the student app.

Registered with the push layer (:func:`vs_notifications.services.push.register_destination`)
when the app loads. It recognises one notification, a confirmed payment
(``billing.payment_received``), and opens that payment's receipt
(``/student/fees/receipts/<payment_id>``).

The receipt opens only when the payment is on the recipient's own pupil record
(the finance account behind it names that pupil), so a link never points a
pupil at somebody else's receipt. A payment that is not theirs, and every other
notification, is passed to the push layer's default.
"""
from __future__ import annotations

from vs_notifications.services.push import PushDestination

from schools.vs_students.constants import STUDENT_APP_PATH


def destination(notification):
    """A :class:`PushDestination` for a pupil's payment push, or None to pass."""
    if notification.event_type.key != "billing.payment_received":
        return None
    payment_id = (notification.metadata or {}).get("payment_id")
    if payment_id in (None, "") or not _is_own_payment(notification.recipient, payment_id):
        return None
    return PushDestination(url=f"{STUDENT_APP_PATH}fees/receipts/{payment_id}")


def _is_own_payment(user, payment_id) -> bool:
    """Whether *payment_id* was made on the account of the pupil *user* signs in as."""
    from schools.core.fal.contracts import SOURCE_TYPE_STUDENT, student_pk
    from schools.vs_students.models import Student
    from vs_finance.models import Payment

    if user is None:
        return False
    row = (
        Payment.objects.filter(pk=payment_id, customer__source_type=SOURCE_TYPE_STUDENT)
        .values_list("customer__source_id", flat=True).first()
    )
    pupil = student_pk(row)
    return pupil is not None and Student.all_objects.filter(pk=pupil, user=user).exists()
