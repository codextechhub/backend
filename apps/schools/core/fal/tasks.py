"""Celery tasks the FAL runs off the request.

``apps/apps/celery.py`` calls ``autodiscover_tasks()``, so this module is
picked up with the app.
"""
from __future__ import annotations

from celery import shared_task
from django.conf import settings

#: Attempts after the first before the account is given up on, and the wait
#: before each (1, 2, 4, 8 then 16 minutes, about half an hour in all).
TRANSFER_ACCOUNT_RETRIES = 5
TRANSFER_ACCOUNT_FIRST_WAIT = 60


@shared_task(
    bind=True, name="fal.issue_transfer_account",
    max_retries=TRANSFER_ACCOUNT_RETRIES, acks_late=True,
)
def issue_transfer_account(self, customer_id: int):
    """Issue a pupil's permanent transfer account after their first bill posted.

    Queued by :func:`schools.core.fal.transfer_accounts.on_invoice_posted`.
    Safe to run any number of times: a pupil with an account is left with it.
    A provider that does not answer is tried again with a doubling wait, and
    after the last attempt the failure is recorded and the pupil keeps the Pay
    screen's button.

    Where tasks run inline (``CELERY_TASK_ALWAYS_EAGER``) there is one attempt
    and no retry: a retry there would run at once, inside the request of
    whoever posted the bill, instead of waiting.
    """
    from .transfer_accounts import ProviderUnavailable, give_up, issue_after_first_bill

    try:
        return issue_after_first_bill(customer_id)
    except ProviderUnavailable as exc:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or self.request.retries >= self.max_retries:
            return give_up(customer_id)
        raise self.retry(exc=exc, countdown=TRANSFER_ACCOUNT_FIRST_WAIT * 2 ** self.request.retries)


@shared_task(name="fal.send_guardian_invoice", acks_late=True)
def send_guardian_invoice(invoice_id: int):
    """Tell a pupil's primary guardian of their new bill in the parent app.

    Queued by :func:`schools.core.fal.guardian_invoices.on_invoice_posted`.
    Never raises and is not retried: the bill stands either way, and a second
    attempt could tell them twice.
    """
    from .guardian_invoices import tell_primary_guardian

    return tell_primary_guardian(invoice_id)
