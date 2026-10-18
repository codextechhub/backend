"""A pupil's permanent bank-transfer account, issued when they are first billed.

A family pays school fees by transfer into one account number that belongs to
the pupil and never changes. It is issued when the pupil's first bill posts,
whoever raised it (a fee run or a manual invoice), so the number is there by
the time the family opens the bill. The Pay screen's "Get my account number"
(:meth:`~schools.core.fal.adapters.django_finance.DjangoStudentPaymentAdapter.open_transfer_account`)
stays for any pupil this misses.

How it runs:

1. ``vs_finance`` sends :data:`~vs_finance.signals.invoice_posted` once a
   posting commits. :func:`on_invoice_posted` ignores a customer that is not a
   pupil's, or that already has an active account, and otherwise queues
   :func:`schools.core.fal.tasks.issue_transfer_account`. The bill is never
   held up or failed by any of it.
2. The task runs :func:`issue_after_first_bill`, which issues through the same
   adapter method as the Pay screen. That method locks the pupil's AR account
   first, so a second bill, a repeated task and the pupil's own button racing
   it all end with one account.

Where it does nothing, it says nothing to the pupil, whose screen goes on
answering as before (``NONE``, or ``UNAVAILABLE`` with its sentence once they
ask):

* the school does not let pupils pay their own fees;
* the provider is not configured, issues no dedicated accounts, or the
  pupil's branch is not set up to take payments directly;
* the family's details the provider needs are missing. This one is recorded
  as a payment event naming what is missing (never a value), once per
  customer while it stays the same, and logged with ids only, so the school
  can be told to fill them in.

**The family's email.** A pupil's AR account is opened from the roll with no
billing email, so the account is opened with the email of the pupil's primary
guardian where the AR account has none (owner decision of 18 October 2026).
It is sent to the provider and copied nowhere: the AR account's billing email,
and so where invoices are emailed, is unchanged.

**One email, one pupil.** The provider keeps one customer per email, for the
whole platform, and a dedicated account belongs to that customer. Siblings
share their primary guardian, and a parent may have children at two schools,
so the second child's account would land on the first child's customer, where
the provider either refuses it or answers with the first child's number. An
email that already opened an active account for another pupil, at any school,
is therefore not used again: that pupil's account is opened without the
family's email, exactly as the Pay screen's button opens one, the adapter
giving the provider a stand-in address of its own (owner decision of 18
October 2026). The number is the pupil's either way. Checking and issuing hold
a lock on the email, so two siblings billed at once are decided one after the
other.

A provider that fails is retried with backoff a bounded number of times
(:mod:`schools.core.fal.tasks`); each failed attempt stores nothing, so the
pupil sees ``NONE`` rather than a half-made account, and the final failure is
recorded like a missing detail.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: What :func:`issue_after_first_bill` did, for the task's result and the tests.
ISSUED = "ISSUED"
ALREADY_ISSUED = "ALREADY_ISSUED"
NOT_A_PUPIL = "NOT_A_PUPIL"
PAYMENTS_OFF = "PAYMENTS_OFF"
NOT_OFFERED = "NOT_OFFERED"
MISSING_DETAILS = "MISSING_DETAILS"
PROVIDER_FAILED = "PROVIDER_FAILED"


class ProviderUnavailable(Exception):
    """The provider did not answer; the task tries again later."""


def _pupil_customer(customer_id):
    """The pupil's AR account behind ``customer_id``, or None for anyone else.

    A customer is a pupil's when it carries a pupil's reference and that pupil
    is on the same school's roll; a family imported before the roll, or a
    reference naming nobody, is not.
    """
    from vs_finance.models import Customer

    from .adapters.django_finance import _student_row
    from .contracts import SOURCE_TYPE_STUDENT

    customer = (
        Customer.objects.filter(pk=customer_id, source_type=SOURCE_TYPE_STUDENT)
        .select_related("entity__tenant").first()
    )
    if customer is None:
        return None
    row = _student_row(customer.source_id)
    if row is None or row["tenant_id"] != customer.entity.tenant_id:
        return None
    return customer


def on_invoice_posted(sender, invoice, **kwargs):
    """Queue a pupil's account when a bill of theirs posts and they have none.

    Connected in :meth:`schools.core.fal.apps.FalConfig.ready`. Runs after the
    posting committed; the checks here are the cheap ones, and the task
    repeats them under its lock.
    """
    from vs_payments.constants import VirtualAccountStatus
    from vs_payments.models import VirtualAccount

    from .contracts import SOURCE_TYPE_STUDENT
    from .tasks import issue_transfer_account

    if invoice.customer.source_type != SOURCE_TYPE_STUDENT:
        return
    if VirtualAccount.objects.filter(
        customer_id=invoice.customer_id, status=VirtualAccountStatus.ACTIVE,
    ).exists():
        return
    issue_transfer_account.delay(invoice.customer_id)


def _record_not_issued(customer, reason, *, missing=()):
    """Record once why ``customer`` has no account, and log it with ids only.

    A further bill with the same reason records nothing more, so the payment
    events read as one entry per problem rather than one per bill.
    """
    from vs_payments import audit
    from vs_payments.constants import PaymentAuditAction
    from vs_payments.models import PaymentEvent

    missing = list(missing)
    logger.warning(
        "transfer account not issued: customer=%s entity=%s reason=%s missing=%s",
        customer.pk, customer.entity_id, reason, ",".join(missing) or "-",
    )
    already = PaymentEvent.objects.filter(
        entity_id=customer.entity_id, action=PaymentAuditAction.VIRTUAL_ACCOUNT_NOT_ISSUED,
        metadata__customer_id=customer.pk, metadata__reason=reason, metadata__missing=missing,
    ).exists()
    if already:
        return
    words = {MISSING_DETAILS: f"missing {', '.join(missing)}",
             PROVIDER_FAILED: "the provider did not answer"}
    audit.record(
        action=PaymentAuditAction.VIRTUAL_ACCOUNT_NOT_ISSUED, entity=customer.entity,
        succeeded=False,
        message=f"Virtual account not issued for {customer.code}: {words[reason]}.",
        metadata={"customer_id": customer.pk, "reason": reason, "missing": missing},
    )


def _family_email(customer) -> str:
    """The address the account is opened with: the billing email, else the primary guardian's."""
    from schools.vs_students.models import StudentGuardian

    from .contracts import student_pk

    if customer.billing_email.strip():
        return customer.billing_email.strip()
    link = (
        StudentGuardian.all_objects.filter(
            student_id=student_pk(customer.source_id), tenant_id=customer.entity.tenant_id,
            is_primary=True,
        )
        .select_related("guardian").first()
    )
    return (link.guardian.email.strip() if link is not None else "")


def _email_in_use(email, customer) -> bool:
    """Whether ``email`` already opened an active account for another pupil, at any school.

    An account counts as opened with it when its AR account's billing email is
    it, or when its AR account has none and its pupil's primary guardian has
    it. An account opened without the family's email (from the Pay screen, or
    a sibling's) counts too: the cost is a stand-in address at the provider,
    never a shared customer.
    """
    from django.db.models import Q
    from django.db.models.functions import Lower

    from schools.vs_students.models import StudentGuardian
    from vs_payments.constants import VirtualAccountStatus
    from vs_payments.models import VirtualAccount
    from vs_payments.services import resolve_provider_name

    from .contracts import SOURCE_TYPE_STUDENT

    guarded = [
        str(pk) for pk in StudentGuardian.all_objects
        .annotate(guardian_email=Lower("guardian__email"))
        .filter(is_primary=True, guardian_email=email.lower())
        .values_list("student_id", flat=True)
    ]
    return (
        VirtualAccount.objects.filter(
            provider=resolve_provider_name(None), status=VirtualAccountStatus.ACTIVE,
        )
        .filter(
            Q(customer__billing_email__iexact=email)
            | Q(customer__billing_email="", customer__source_type=SOURCE_TYPE_STUDENT,
                customer__source_id__in=guarded)
        )
        .exclude(customer__source_type=SOURCE_TYPE_STUDENT, customer__source_id=customer.source_id)
        .exists()
    )


def _lock_email(email) -> None:
    """Hold ``email`` until the transaction ends, so its check and its issue are one step."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            [f"transfer-account-email:{email.lower()}"],
        )


def issue_after_first_bill(customer_id) -> str:
    """Issue the pupil's account behind ``customer_id`` where the school can, once.

    Returns one of the outcomes above. Raises :class:`ProviderUnavailable`
    when the provider did not answer, having stored nothing. Must run with no
    transaction open: the provider is called inside a short transaction of its
    own, which holds the family's email and the pupil's AR account and nothing
    of the bill.
    """
    from django.db import transaction

    from vs_payments.exceptions import ProviderNotConfiguredError
    from vs_payments.services import virtual_account_missing_details

    from .adapters.django_finance import DjangoStudentPaymentAdapter
    from .contracts import Unavailable
    from .student_payments import read_student_payment_rules

    customer = _pupil_customer(customer_id)
    if customer is None:
        return NOT_A_PUPIL
    if not read_student_payment_rules(customer.entity.tenant).enabled:
        return PAYMENTS_OFF
    payments = DjangoStudentPaymentAdapter()
    current = payments.transfer_account(customer.source_id)
    if current.is_available and current.value is not None:
        return ALREADY_ISSUED
    email = _family_email(customer)
    try:
        missing = virtual_account_missing_details(customer, email=email)
    except ProviderNotConfiguredError:
        return NOT_OFFERED
    if missing:
        _record_not_issued(customer, MISSING_DETAILS, missing=missing)
        return MISSING_DETAILS

    with transaction.atomic():
        if email:
            _lock_email(email)
            if _email_in_use(email, customer):
                email = ""  # A sibling's: opened as the Pay screen opens one.
        result = payments.issue_transfer_account(
            customer.source_id, actor=None, customer_email=email,
        )
    if result.is_available:
        return ISSUED
    if result.reason == Unavailable.NOT_CONFIGURED:
        return NOT_OFFERED
    raise ProviderUnavailable(str(result.reason))


def give_up(customer_id) -> str:
    """Record that the provider never answered for ``customer_id``'s account."""
    customer = _pupil_customer(customer_id)
    if customer is not None:
        _record_not_issued(customer, PROVIDER_FAILED)
    return PROVIDER_FAILED
