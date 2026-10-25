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
    from .transfer_accounts import ProviderUnavailable, give_up, issue_for_customer

    try:
        return issue_for_customer(customer_id)
    except ProviderUnavailable as exc:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or self.request.retries >= self.max_retries:
            return give_up(customer_id)
        raise self.retry(exc=exc, countdown=TRANSFER_ACCOUNT_FIRST_WAIT * 2 ** self.request.retries)


@shared_task(
    bind=True, name="fal.issue_pupil_transfer_account",
    max_retries=TRANSFER_ACCOUNT_RETRIES, acks_late=True,
)
def issue_pupil_transfer_account(self, student_id: int):
    """Issue a pupil's transfer account when they join the roll.

    Queued by the status machine once a pupil becomes enrolled. Opens the
    pupil's finance account first if the school has not billed them yet, then
    issues the number as :func:`issue_transfer_account` does, with the same
    backoff and the same last-attempt record. Safe to run any number of times.
    """
    from .transfer_accounts import ProviderUnavailable, give_up, issue_for_pupil

    try:
        return issue_for_pupil(student_id)
    except ProviderUnavailable as exc:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or self.request.retries >= self.max_retries:
            return give_up(exc.customer_id)
        raise self.retry(exc=exc, countdown=TRANSFER_ACCOUNT_FIRST_WAIT * 2 ** self.request.retries)


@shared_task(name="fal.issue_missing_transfer_accounts", acks_late=True)
def issue_missing_transfer_accounts():
    """Issue an account to every pupil on the roll who has none, a bounded batch a day.

    Run daily by beat. See
    :func:`schools.core.fal.transfer_accounts.sweep_missing_accounts` for what
    it skips and why it cannot hammer a provider that is down or has not
    approved a school.
    """
    from .transfer_accounts import sweep_missing_accounts

    return sweep_missing_accounts()


@shared_task(name="fal.send_guardian_invoice", acks_late=True)
def send_guardian_invoice(invoice_id: int):
    """Tell a pupil's primary guardian of their new bill in the parent app.

    Queued by :func:`schools.core.fal.guardian_invoices.on_invoice_posted`.
    Never raises and is not retried: the bill stands either way, and a second
    attempt could tell them twice.
    """
    from .guardian_invoices import tell_primary_guardian

    return tell_primary_guardian(invoice_id)
