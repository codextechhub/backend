"""A pupil's permanent bank-transfer account, issued so every pupil on the roll has one.

A family pays school fees by transfer into one account number that belongs to
the pupil and is meant to stay theirs, so every pupil on the roll is meant to
have one before the family needs it. Three routes issue it, all through the
same adapter method as the Pay screen's "Get my account number"
(:meth:`~schools.core.fal.adapters.django_finance.DjangoStudentPaymentAdapter.open_transfer_account`),
which stays for any pupil they miss:

1. **The first bill.** ``vs_finance`` sends :data:`~vs_finance.signals.invoice_posted`
   once a posting commits. :func:`on_invoice_posted` ignores a customer that
   is not a pupil's, or that already has an active account, and otherwise
   queues :func:`schools.core.fal.tasks.issue_transfer_account`. The bill is
   never held up or failed by any of it.
2. **Joining the roll.** A pupil who becomes enrolled (admitted, confirmed
   from applicant, or readmitted) is queued by the status machine, and
   :func:`issue_for_pupil` opens their finance account if they have none and
   issues the number, so the account exists before the first bill.
3. **The daily sweep.** :func:`sweep_missing_accounts` finds every pupil on the
   roll without an active account and issues one, a bounded number per run.
   It is the net under the other two: a provider that was down, a school whose
   payments were switched on later, a family whose email was filled in since.
   It does not hammer: a school or branch the provider will not serve is
   asked once per run, and a school whose provider keeps failing is left
   alone for the rest of it.

All routes lock the pupil's finance account first, so a second bill, a
repeated task, the sweep and the pupil's own button racing it all end with one
account.

**Whether the pupil may pay has no say.** ``fees.student_payment_enabled``
decides what a pupil may do and see in the student portal; it does not stop
an account being issued, because a parent pays into it.

Where an account cannot be issued, nothing is said to the family, whose screen
goes on answering as before (``NONE``, or ``UNAVAILABLE`` with its sentence
once they ask):

* the provider is not configured, issues no dedicated accounts, or the
  pupil's branch is not set up to take payments directly;
* the school has no books yet to open the pupil's finance account in;
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

**When the number changes.** The payments engine replaces a number the
provider closed or gave to somebody else
(:mod:`vs_payments.virtual_account_standing`), and every number when a school's
money starts settling to its own bank, and sends
:data:`~vs_payments.signals.virtual_account_replaced`.
:func:`on_virtual_account_replaced` tells the pupil (in the app, and a push
that names no number) and their primary guardian (in the app, else by email)
not to pay into the old number. The guardian is told where to see the new one,
never the number itself, and that the school never sends one by email (owner
decision of 19 October 2026): a family taught to trust a number in an email is
a family a fraudster's copy of it can redirect.

A provider that fails is retried with backoff a bounded number of times
(:mod:`schools.core.fal.tasks`); each failed attempt stores nothing, so the
pupil sees ``NONE`` rather than a half-made account, and the final failure is
recorded like a missing detail. The sweep tries it again the next day.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: What :func:`issue_for_customer` and :func:`issue_for_pupil` did, for the task's result and the tests.
ISSUED = "ISSUED"
ALREADY_ISSUED = "ALREADY_ISSUED"
NOT_A_PUPIL = "NOT_A_PUPIL"
NOT_ON_ROLL = "NOT_ON_ROLL"
NO_BOOKS = "NO_BOOKS"
NOT_OFFERED = "NOT_OFFERED"
MISSING_DETAILS = "MISSING_DETAILS"
PROVIDER_FAILED = "PROVIDER_FAILED"

#: Provider asks the sweep makes in one run, so a school with thousands of
#: pupils and no accounts is worked through over several days rather than
#: in one burst. Pupils the sweep passes over without asking the provider
#: (missing details, no books) do not count.
SWEEP_BATCH = 300

#: Provider failures after which the sweep leaves a school alone for the rest
#: of the run: the provider is down for it, and asking again helps nobody.
SWEEP_FAILURES_BEFORE_SKIPPING_SCHOOL = 3


class ProviderUnavailable(Exception):
    """The provider did not answer; the task tries again later.

    ``customer_id`` names the finance account the attempt was for, so the final
    failure can be recorded against it.
    """

    def __init__(self, reason="", customer_id=None):
        super().__init__(reason)
        self.customer_id = customer_id


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


EVENT_PUPIL_ACCOUNT_CHANGED = "billing.transfer_account_changed"
EVENT_GUARDIAN_ACCOUNT_CHANGED = "billing.guardian_transfer_account_changed"


def on_virtual_account_replaced(sender, old, new=None, **kwargs):
    """Tell a pupil and their primary guardian that the pupil's account number changed.

    Connected in :meth:`schools.core.fal.apps.FalConfig.ready`; runs after the
    replacement committed. A customer that is not a pupil's is left alone.
    Each message is sent on its own, so one that cannot go never stops the other.
    """
    from schools.vs_students.models import Student, StudentGuardian
    from vs_notifications.notify import UnregisteredRecipient, send_notification
    from vs_rbac.scoping import only_branch_id_or_several

    from .contracts import student_pk

    customer = _pupil_customer(old.customer_id) if old.customer_id else None
    if customer is None:
        return
    student = (
        Student.all_objects.select_related("user", "branch", "tenant")
        .get(pk=student_pk(customer.source_id))
    )
    tenant, branch = student.tenant, student.branch
    context = {
        "student_first_name": student.first_name,
        "old_account_number": old.account_number,
        "has_new_account": new is not None,
        "tenant_name": tenant.name,
        "branch_name": branch.name if branch and only_branch_id_or_several(tenant) is None else "",
    }

    user = student.user if student.user_id else None
    if user is not None and user.may_sign_in:
        try:
            send_notification(event_key=EVENT_PUPIL_ACCOUNT_CHANGED, context=context,
                              recipients=[user], tenant=tenant, branch=branch)
        except Exception:
            logger.exception("The pupil's account-number notice for %s could not be sent.", student.pk)

    link = (
        StudentGuardian.all_objects.filter(student=student, tenant=tenant, is_primary=True)
        .select_related("guardian__user").first()
    )
    if link is None:
        return
    guardian = link.guardian
    guardian_user = guardian.user if guardian.user_id else None
    email = ((guardian_user.email if guardian_user else "") or guardian.email or "").strip()
    if guardian_user is None and not email:
        return
    try:
        send_notification(
            event_key=EVENT_GUARDIAN_ACCOUNT_CHANGED, context=context,
            recipients=[guardian_user] if guardian_user else [],
            unregistered_recipients=None if guardian_user else [
                UnregisteredRecipient(email=email, name=guardian.full_name)],
            tenant=tenant, branch=branch,
        )
    except Exception:
        logger.exception("The guardian's account-number notice for %s could not be sent.", student.pk)


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


def issue_for_customer(customer_id) -> str:
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

    customer = _pupil_customer(customer_id)
    if customer is None:
        return NOT_A_PUPIL
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
    raise ProviderUnavailable(str(result.reason), customer_id=customer.pk)


def _books_customer(student):
    """The pupil's finance account, opened in their school's books if they have none.

    ``None`` when the school has no books to open it in yet (onboarding not
    finished), or has two sets of books and no way to name the primary one.
    """
    from vs_finance.models import Customer

    from .adapters.django_finance import DjangoStudentCustomerAdapter, _primary_entity
    from .contracts import SOURCE_TYPE_STUDENT
    from .exceptions import CrossTenantError, ProvisioningError

    existing = Customer.objects.filter(
        source_type=SOURCE_TYPE_STUDENT, source_id=str(student.pk),
        entity__tenant_id=student.tenant_id,
    ).order_by("pk").first()
    if existing is not None:
        return existing
    school = getattr(student.tenant, "school_profile", None)
    if school is None:
        return None
    try:
        entity = _primary_entity(school)
        handle = DjangoStudentCustomerAdapter().ensure_customer(
            student.pk, entity_ref=entity.pk,
        ).unwrap()
    except (ProvisioningError, CrossTenantError, ValueError):
        logger.warning("No finance account could be opened for student %s.", student.pk)
        return None
    return Customer.objects.get(pk=handle.customer_ref)


def issue_for_pupil(student_id) -> str:
    """Make sure a pupil on the roll has a transfer account, opening their finance account first.

    The route taken when a pupil joins the roll and by the daily sweep, before
    any bill exists to have opened the finance account. A pupil who has left
    the roll, or is not on this school's roll, is left alone. Raises
    :class:`ProviderUnavailable` like :func:`issue_for_customer`.
    """
    from schools.vs_students.constants import ON_ROLL
    from schools.vs_students.models import Student

    student = Student.all_objects.select_related("tenant__school_profile").filter(pk=student_id).first()
    if student is None or student.status not in ON_ROLL:
        return NOT_ON_ROLL
    customer = _books_customer(student)
    if customer is None:
        return NO_BOOKS
    return issue_for_customer(customer.pk)


def sweep_missing_accounts(*, limit=SWEEP_BATCH) -> dict:
    """Issue an account to every pupil on the roll who has none, up to ``limit`` provider asks.

    Returns a count of each outcome, plus ``skipped`` for pupils passed over
    without being asked: a school or branch the provider already declined in
    this run, and a school whose provider has failed
    :data:`SWEEP_FAILURES_BEFORE_SKIPPING_SCHOOL` times. A provider failure is
    recorded like the retrying task's final failure, once per pupil. Pupils
    are taken in roll order, and one that is issued drops out of the next
    run, so a school with more pupils than the batch is worked through over
    several days.
    """
    from collections import Counter

    from django.db.models import CharField, Exists, OuterRef
    from django.db.models.functions import Cast

    from schools.vs_students.constants import ON_ROLL
    from schools.vs_students.models import Student
    from vs_payments.constants import VirtualAccountStatus
    from vs_payments.models import VirtualAccount

    from .contracts import SOURCE_TYPE_STUDENT

    held = VirtualAccount.objects.filter(
        customer__source_type=SOURCE_TYPE_STUDENT,
        customer__source_id=Cast(OuterRef("pk"), CharField()),
        status=VirtualAccountStatus.ACTIVE,
    )
    pupils = (
        Student.all_objects.filter(status__in=ON_ROLL)
        .annotate(has_account=Exists(held)).filter(has_account=False)
        .order_by("pk").values_list("pk", "tenant_id", "branch_id")
    )
    asked_for = {ISSUED, NOT_OFFERED, PROVIDER_FAILED}
    outcomes = Counter()
    declined, failures, asks = set(), Counter(), 0
    for pk, tenant_id, branch_id in pupils.iterator(chunk_size=500):
        if asks >= limit:
            break
        if (tenant_id, branch_id) in declined or failures[tenant_id] >= SWEEP_FAILURES_BEFORE_SKIPPING_SCHOOL:
            outcomes["skipped"] += 1
            continue
        try:
            outcome = issue_for_pupil(pk)
        except ProviderUnavailable as exc:
            failures[tenant_id] += 1
            outcome = give_up(exc.customer_id)
        except Exception:
            logger.exception("The transfer account sweep failed for student %s.", pk)
            outcome = "ERROR"
        outcomes[outcome] += 1
        if outcome in asked_for:
            asks += 1
        if outcome == NOT_OFFERED:
            declined.add((tenant_id, branch_id))
    return dict(outcomes)


def give_up(customer_id) -> str:
    """Record that the provider never answered for ``customer_id``'s account."""
    customer = _pupil_customer(customer_id)
    if customer is not None:
        _record_not_issued(customer, PROVIDER_FAILED)
    return PROVIDER_FAILED
