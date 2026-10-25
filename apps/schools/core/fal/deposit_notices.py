"""A transfer into a pupil's account number is confirmed to the family that paid it.

A payment started in an app tells whoever pressed Pay when it is confirmed
(:func:`vs_payments.services._tell_the_payer`). A transfer has nobody who
pressed anything, and the payments engine cannot know who in a family sent it,
so it sends :data:`~vs_payments.signals.virtual_account_deposit_booked` and the
school decides who is told.

:func:`on_virtual_account_deposit_booked` tells the pupil, where they have a
portal account and the school lets pupils see their payments, and every
guardian of the pupil who has a parent app account, in the app and by push
(``billing.payment_received``). The in-app notice names the pupil and the amount;
the push names neither, so nothing shows on a locked screen. A tap opens the
receipt (:mod:`schools.vs_parent_portal.push` and
:mod:`schools.vs_student_portal.push`). No email goes: the receipt email already
reaches the pupil's billing address and primary guardian, and a second email for
the same money would only repeat it.

Only the pupil's own family is told: the recipients come from the pupil the
payment's finance account belongs to, inside that account's own school. A
deposit onto an account that is not a pupil's tells nobody here.
Each recipient is sent to on its own and a failure is logged, so one account
that cannot be reached never stops the others and never undoes the booking.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EVENT_PAYMENT_RECEIVED = "billing.payment_received"


def on_virtual_account_deposit_booked(sender, intent, **kwargs):
    """Tell the pupil and their guardians that a transfer into the pupil's account was booked.

    Connected in :meth:`schools.core.fal.apps.FalConfig.ready`; runs after the
    booking committed.
    """
    from schools.vs_students.models import Student, StudentGuardian
    from vs_finance.document_email import receipt_notice_context
    from vs_notifications.constants import ChannelChoices
    from vs_notifications.notify import send_notification

    from .contracts import student_pk
    from .student_payments import read_student_payment_rules
    from .transfer_accounts import _pupil_customer

    payment = intent.payment
    if payment is None:
        return
    customer = _pupil_customer(payment.customer_id)
    if customer is None:
        return
    student = (
        Student.all_objects.select_related("user", "tenant")
        .get(pk=student_pk(customer.source_id))
    )
    tenant = student.tenant

    recipients = []
    pupil = student.user if student.user_id else None
    if pupil is not None and pupil.may_sign_in and read_student_payment_rules(tenant).enabled:
        recipients.append(pupil)
    for link in StudentGuardian.all_objects.filter(
        student=student, tenant=tenant,
    ).select_related("guardian__user").order_by("pk"):
        account = link.guardian.user if link.guardian.user_id else None
        if account is not None and account.may_sign_in and account not in recipients:
            recipients.append(account)

    context = receipt_notice_context(payment)
    metadata = {
        "payment_id": payment.pk, "collection_reference": intent.reference,
        "student_id": student.pk,
    }
    for account in recipients:
        try:
            send_notification(
                event_key=EVENT_PAYMENT_RECEIVED, context=context, recipients=[account],
                tenant=tenant, branch=payment.branch, metadata=metadata,
                only_channels=[ChannelChoices.IN_APP, ChannelChoices.PUSH],
            )
        except Exception:
            logger.exception(
                "The payment notice for collection %s to account %s could not be sent.",
                intent.reference, account.pk,
            )
