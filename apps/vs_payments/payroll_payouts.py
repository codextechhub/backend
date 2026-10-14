"""Staff pay sent through the payment provider.

A tenant whose payroll route is ``PAYSTACK`` (``vs_finance`` payroll settings)
pays a posted payroll run by transfer instead of the bursar paying from the
bank and recording it. Paying the run (:func:`pay_run`) makes one ``PAYROLL``
payout batch per branch, one instruction per payroll line for its net pay, into
the staff member's verified or confirmed bank account (:mod:`vs_finance.staff_banking`),
and hands each batch to a worker (:func:`dispatch_payroll_batch`).

**No payout approval.** The run was approved by its own route (a second person,
never its preparer), then posted, before it could be paid, and that is the
control. A run posted before payroll approvals existed has no approval, so it is
never sent through the provider; it can be recorded as paid from the branch's bank. A payroll batch is therefore authorised by its run
(:func:`assert_run_authorises`) and by nothing the caller passes: the exemption
is decided by the batch's purpose inside the one choke point every transfer goes
through (:func:`vs_payments.services._dispatch_transfer`), so a vendor or
settlement batch can never use it.

**Each branch pays from its own money.** A branch's batch draws on the money the
platform holds for that branch. If the branch holds less than its whole batch
needs, nothing of that batch is sent: its lines read ``WAITING_FOR_FUNDS`` and
the sweep (:func:`sweep_payroll_batches`) tries again as online payments clear.
Other branches' batches are unaffected. Lekki holds N900,000 for a N1,200,000
payroll and Ikeja holds N2,000,000 for N1,500,000: Ikeja's staff are paid today
and Lekki's wait, with no half-paid branch.

**The platform's own payroll.** The platform's books (the tenant of kind
``PLATFORM``) pay their staff the same way, from the platform's own share of the
provider balance instead of a branch's held money
(:func:`vs_payments.held.own_share_available`). The platform has no custody mode,
so the rule that a tenant's money must be held does not apply to it
(:func:`assert_route_allows`). A payroll larger than the share waits for funds
like a short branch, and its bursar may record it as paid from the bank.

**Booking.** When the provider confirms a transfer, the line is booked
``Dr net wages payable, Cr gateway clearing`` in the line's branch (money that
left the provider balance is the branch's gateway clearing in its books, as for
a supplier paid online), and the provider's transfer fee the branch bears is
booked beside it (:func:`vs_payments.held.book_transfer_fee`). A failed or
reversed transfer gives the reservation back and the line reads ``FAILED``,
ready to be retried. The run reads paid once every line is paid, whether through
the provider or recorded from the branch's own bank.

A line whose staff member has no verified or confirmed bank account is not sent:
it reads ``NEEDS_BANK_DETAILS``, and can be paid later through the provider or
recorded as paid from the branch's bank.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_finance.exceptions import FinanceError
from vs_finance.wording import counted

from . import audit, custody, held
from .constants import (
    PaymentAuditAction,
    PayoutBatchStatus,
    PayoutPurpose,
    PayoutStatus,
)
from .exceptions import PaymentStateError, PayoutApprovalRequiredError
from .models import PayoutBatch, PayoutInstruction

logger = logging.getLogger("vs_payments.payroll_payouts")


#: What a run posted before payroll approvals existed is told when paid through the provider.
UNAPPROVED_RUN = (
    "Approve this run before paying it through Paystack. A run posted before payroll "
    "approvals existed cannot be approved now, so record it as paid from the branch's bank "
    "instead."
)


class PayrollDestinationChanged(PaymentStateError):
    """A staff member's bank details changed after their payment was prepared (409)."""

    error_code = "PAYROLL_DESTINATION_CHANGED"
    default_message = ("The staff member's bank account changed after this payment was "
                       "prepared. Pay the line again.")


# --------------------------------------------------------------------------- #
# What authorises a payroll payout                                             #
# --------------------------------------------------------------------------- #

def payment_route(entity) -> str:
    """The tenant's payroll payment route (``OWN_BANK`` by default)."""
    from vs_finance.payroll_statutory import payroll_settings

    return payroll_settings(entity).payment_route


def run_for_batch(batch):
    """The payroll run a ``PAYROLL`` batch pays, or None when it names none of this entity's."""
    from vs_finance.models import PayrollRun

    run_id = (batch.metadata or {}).get("payroll_run_id")
    if not run_id:
        return None
    return PayrollRun.objects.filter(pk=run_id, entity_id=batch.entity_id).first()


def assert_run_authorises(batch) -> None:
    """Refuse a payroll batch unless the payroll run it pays is posted and paid through the provider.

    This is the whole authority a payroll payout needs: the run passed its own
    approval and was posted, and the tenant pays its runs through the provider.
    Raises :class:`PayoutApprovalRequiredError` otherwise.
    """
    from vs_finance.approvals import payroll_run_approved
    from vs_finance.constants import PayrollPaymentRoute, PayrollRunStatus

    run = run_for_batch(batch) if batch.purpose == PayoutPurpose.PAYROLL else None
    if run is None or run.run_status != PayrollRunStatus.POSTED:
        raise PayoutApprovalRequiredError(
            "A payroll payout can be sent only for a posted payroll run.")
    if not payroll_run_approved(run):
        raise PayoutApprovalRequiredError(UNAPPROVED_RUN)
    if payment_route(run.entity) != PayrollPaymentRoute.PAYSTACK:
        raise PayoutApprovalRequiredError(
            "This tenant no longer pays payroll through the payment provider.")


def assert_route_allows(entity) -> None:
    """Refuse paying staff through the provider unless there is balance to pay them from (409).

    A tenant needs its online money held for its branches. The platform's own
    books (:func:`vs_payments.held.is_platform_entity`) have no custody mode:
    the custody rule does not apply to them, and they pay from their own share of
    the provider balance instead (:func:`vs_payments.held.own_share_available`).
    """
    if held.is_platform_entity(entity):
        return
    custody.assert_online_payouts_allowed(entity)
    if not held.holds_for(entity):
        raise PaymentStateError(
            "These books keep no money with the platform, so staff cannot be paid "
            "through the payment provider.")


# --------------------------------------------------------------------------- #
# Lines                                                                        #
# --------------------------------------------------------------------------- #

def _line_of(payout, *, lock=False):
    from vs_finance.models import PayrollLine

    line_id = (payout.metadata or {}).get("payroll_line_id")
    qs = PayrollLine.objects.filter(pk=line_id)
    return (qs.select_for_update(of=("self",)) if lock else qs).first() if line_id else None


def _set_line(payout, **fields) -> None:
    from vs_finance.models import PayrollLine

    line_id = (payout.metadata or {}).get("payroll_line_id")
    if line_id:
        PayrollLine.objects.filter(pk=line_id).update(updated_at=timezone.now(), **fields)


def bank_problem(employee_id) -> str:
    """Why a staff member cannot be paid through the provider, or "" when they can."""
    from vs_finance.models import StaffBankAccount

    if not employee_id:
        return "This line has no staff account to hold bank details."
    account = StaffBankAccount.objects.filter(employee_id=employee_id).first()
    if account is None:
        return "No bank account has been saved for this staff member."
    if not account.is_payable:
        return f"Bank account: {account.get_verification_state_display().lower()}."
    return ""


def validate_line_destination(payout, *, lock=False):
    """Refuse a payroll transfer whose line or destination is no longer what was prepared.

    The instruction was written with the staff member's account number and bank
    code. A change to either after the payment was prepared, an account that is no
    longer verified or confirmed, a line already paid or failed, or a run no
    longer posted would otherwise send money where nobody checked.
    """
    from vs_finance.constants import PayrollLinePayState, PayrollRunStatus
    from vs_finance.models import StaffBankAccount

    line = _line_of(payout, lock=lock)
    if line is None or line.pay_state not in (
            PayrollLinePayState.SENT, PayrollLinePayState.WAITING_FOR_FUNDS):
        raise PaymentStateError("This payroll line is no longer waiting to be paid.")
    if line.run.run_status != PayrollRunStatus.POSTED:
        raise PaymentStateError("This payroll run is no longer posted.")
    qs = StaffBankAccount.objects.filter(employee_id=line.employee_id)
    account = (qs.select_for_update() if lock else qs).first()
    if (account is None or not account.is_payable
            or account.bank_code != payout.beneficiary_bank_code
            or account.account_number != payout.beneficiary_account_number):
        raise PayrollDestinationChanged()
    return line


def mark_waiting(payout, exc) -> None:
    """A branch holds too little for ``payout``: its line and batch wait for funds."""
    from vs_finance.constants import PayrollLinePayState

    message = str(getattr(exc, "message", exc))[:255]
    _set_line(payout, pay_state=PayrollLinePayState.WAITING_FOR_FUNDS, pay_note=message)
    _set_batch_waiting(payout.batch_id, True)


def _set_batch_waiting(batch_id, waiting: bool) -> None:
    from . import services

    with transaction.atomic():
        batch = PayoutBatch.objects.select_for_update().get(pk=batch_id)
        batch.metadata = {**(batch.metadata or {}), "waiting_for_funds": waiting}
        batch.save(update_fields=["metadata", "updated_at"])
        services._recompute_batch_status(batch)


def line_failed(payout, reason="") -> None:
    """The transfer for ``payout`` ended without paying: its line reads FAILED and can be retried."""
    from vs_finance.constants import PayrollLinePayState

    _set_line(payout, pay_state=PayrollLinePayState.FAILED, pay_note=str(reason or "")[:255])


def book_line_paid(payout, *, paid_at=None, actor_user=None) -> None:
    """Book a paid payroll transfer: ``Dr net wages payable, Cr gateway clearing``, once.

    In the line's branch, dated by the day the money left (never before the
    run's own date), for what the provider sent. Marks the line paid, issues its
    payslip, and reads the run (and the line's branch share) paid once nothing
    else is owed. The provider's transfer fee is booked by the caller's shared
    step (:func:`vs_payments.held.book_transfer_fee`).
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.accounts import resolve_account
    from vs_finance.audit import record
    from vs_finance.constants import (
        AccountMappingKey,
        FinanceAuditAction,
        JournalSource,
        NET_WAGES_PAYABLE_CODE,
        PayrollLinePaidVia,
        PayrollLinePayState,
    )
    from vs_finance.models import JournalEntry, JournalLine
    from vs_finance.payslips import issue_payslips
    from vs_finance.posting import post_journal, resolve_period

    from .services import _booking_date

    line = _line_of(payout, lock=True)
    if line is None:
        raise PaymentStateError("This payout line pays no payroll line.")
    if line.pay_state == PayrollLinePayState.PAID:
        return
    run = line.run
    entity = run.entity
    amount = int(payout.amount)
    branch_id = held.held_branch_id(entity, line.branch_id)
    booked_on, dating = _booking_date(entity, paid_at, branch=branch_id)
    day = max(booked_on, run.pay_date)
    if dating:
        payout.metadata = {**(payout.metadata or {}), **dating}
    net_payable = run.net_payable_account or resolve_account(
        entity, NET_WAGES_PAYABLE_CODE, label="net wages payable")
    entry = JournalEntry.objects.create(
        entity=entity, branch_id=branch_id, date=day, period=resolve_period(entity, day),
        source=JournalSource.BANK, currency=run.currency, reference=payout.reference,
        narration=f"Pay net wages {run.period_label or run.document_number or ''}".strip()[:255],
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=net_payable, debit=amount, credit=0, line_no=1,
        description="Net wages payable")
    JournalLine.objects.create(
        entry=entry, debit=0, credit=amount, line_no=2, description="Gateway clearing",
        account=resolve_mapped_account(
            entity, AccountMappingKey.GATEWAY_CLEARING, label="gateway clearing"))
    post_journal(entry, actor_user=actor_user)

    line.pay_state = PayrollLinePayState.PAID
    line.paid_via = PayrollLinePaidVia.PAYSTACK
    line.pay_note = ""
    line.payout_reference = payout.reference
    line.save(update_fields=["pay_state", "paid_via", "pay_note", "payout_reference", "updated_at"])
    record(
        entity=entity, action=FinanceAuditAction.PAYROLL_PAID, actor_user=actor_user,
        target=run, branch=branch_id,
        message=f"Paid a member of staff through the payment provider ({payout.reference}).",
        journal_id=entry.pk, branch_id=branch_id, line_id=line.pk,
    )
    issue_payslips(run, [line], actor_user=actor_user)
    refresh_run_paid(run)


def refresh_run_paid(run) -> None:
    """Read a branch's share, and the run, paid once every line they cover is paid."""
    from vs_finance.constants import PayrollLinePayState, PayrollRunStatus
    from vs_finance.models import PayrollRun

    run = PayrollRun.objects.select_for_update().get(pk=run.pk)
    for share in run.branch_shares.select_for_update():
        if share.status == PayrollRunStatus.POSTED and not run.lines.filter(
                branch_id=share.branch_id).exclude(pay_state=PayrollLinePayState.PAID).exists():
            share.status = PayrollRunStatus.PAID
            share.save(update_fields=["status", "updated_at"])
    if run.run_status == PayrollRunStatus.POSTED and not run.lines.exclude(
            pay_state=PayrollLinePayState.PAID).exists():
        run.run_status = PayrollRunStatus.PAID
        run.save(update_fields=["run_status", "updated_at"])


# --------------------------------------------------------------------------- #
# Paying a run                                                                 #
# --------------------------------------------------------------------------- #

def pay_run(run, *, actor_user=None) -> dict:
    """Send every line of a posted run that is not yet sent, through the provider.

    Idempotent: a line already sent, waiting for funds or paid is left alone, so
    calling it again after fixing a staff member's bank details sends only the
    lines that were held back.
    """
    return send_run(run, actor_user=actor_user, retry=False)


def retry_run(run, *, actor_user=None, line_ids=None) -> dict:
    """Send again the lines of a run whose transfer failed, and any still needing bank details."""
    return send_run(run, actor_user=actor_user, retry=True, line_ids=line_ids)


def send_run(run, *, actor_user=None, retry=False, line_ids=None) -> dict:
    """Make one payroll batch per branch for the lines that can be sent, and queue their dispatch.

    ``retry`` selects the lines whose last transfer failed; otherwise the lines
    never sent. Either way a line needing bank details is looked at again. A line
    with no payable bank account is marked ``NEEDS_BANK_DETAILS`` and not sent; a
    line of nothing to pay is marked paid. Raises :class:`PayrollError` unless the
    run is posted and there is balance to pay from (:func:`assert_route_allows`),
    and a 400 for a ``line_ids`` entry that is not a sendable line of this run. Returns the counts per state
    the call left lines in, and the batches made.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.approvals import payroll_run_approved
    from vs_finance.audit import record
    from vs_finance.constants import (
        AccountMappingKey,
        FinanceAuditAction,
        PayrollLinePayState as State,
        PayrollPaymentRoute,
        PayrollRunStatus,
    )
    from vs_finance.exceptions import PayrollError
    from vs_finance.models import PayrollRun

    from . import services

    entity = run.entity
    if payment_route(entity) != PayrollPaymentRoute.PAYSTACK:
        raise PayrollError("This tenant pays payroll from the branch's bank, not through the provider.")
    if not payroll_run_approved(run):
        raise PayrollError(UNAPPROVED_RUN)
    assert_route_allows(entity)
    provider = services.resolve_provider_name(None)
    services.get_provider(provider)
    resolve_mapped_account(entity, AccountMappingKey.GATEWAY_CLEARING, label="gateway clearing")
    resolve_mapped_account(entity, AccountMappingKey.BANK_CHARGES, label="bank charges")

    sendable = {State.FAILED, State.NEEDS_BANK_DETAILS} if retry else {"", State.NEEDS_BANK_DETAILS}
    wanted = {int(i) for i in line_ids} if line_ids else None
    batches, states = [], {}
    with transaction.atomic():
        run = PayrollRun.objects.select_for_update().get(pk=run.pk)
        if run.run_status != PayrollRunStatus.POSTED:
            raise PayrollError(
                f"Payroll run {run.document_number or run.pk} must be posted and unpaid to be paid.")
        paid_branches = {s.branch_id for s in run.branch_shares.filter(status=PayrollRunStatus.PAID)}
        lines = list(run.lines.select_for_update(of=("self",)).select_related("branch").order_by("line_no", "id"))
        chosen = [
            line for line in lines
            if line.pay_state in sendable and line.branch_id not in paid_branches
            and (wanted is None or line.pk in wanted)
        ]
        if wanted is not None and {line.pk for line in chosen} != wanted:
            raise ValidationError({"line_ids": "Some lines are not in this run or cannot be sent now."})
        if not chosen:
            raise PayrollError(
                "Nothing is left to send: every line is already sent, waiting for funds or paid.")
        by_branch: dict = {}
        for line in chosen:
            if line.branch_id is None:
                raise PayrollError("A line of this run has no branch, so it cannot be paid.")
            by_branch.setdefault(line.branch_id, []).append(line)
        for branch_id in sorted(by_branch):
            batch = _make_branch_batch(run, branch_id, by_branch[branch_id], provider, actor_user)
            if batch is not None:
                batches.append(batch)
        for line in chosen:
            line.refresh_from_db(fields=["pay_state"])
            states[line.pay_state] = states.get(line.pay_state, 0) + 1
        refresh_run_paid(run)  # Lines with nothing to pay may be the last unpaid ones.
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_PAYOUTS_SENT, actor_user=actor_user,
            target=run, branch=run.branch_id,
            message=(f"Sent payroll run {run.document_number or run.pk} through the payment "
                     f"provider in {counted(len(batches), 'branch batch', 'branch batches')}."),
            lines=len(chosen), retry=retry,
        )
        batch_ids = [batch.pk for batch in batches]
        actor_id = getattr(actor_user, "pk", None)
        transaction.on_commit(lambda: enqueue_dispatch(batch_ids, actor_id))
    return {"batches": [batch.reference for batch in batches], "states": states}


def _make_branch_batch(run, branch_id, lines, provider, actor_user):
    """One branch's payroll batch for the sendable ``lines``; the rest are marked as they stand."""
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.constants import AccountMappingKey, PayrollLinePayState as State
    from vs_finance.models import StaffBankAccount

    from .services import _entity_currency, _new_reference

    entity = run.entity
    accounts = {
        a.employee_id: a for a in StaffBankAccount.objects.filter(
            employee_id__in=[l.employee_id for l in lines if l.employee_id])
    }
    payable, total = [], 0
    for line in lines:
        net = int(line.net_amount)
        account = accounts.get(line.employee_id)
        if net <= 0:
            line.pay_state, line.pay_note = State.PAID, "Nothing to pay."
        elif account is None or not account.is_payable:
            line.pay_state, line.pay_note = State.NEEDS_BANK_DETAILS, bank_problem(line.employee_id)
        else:
            payable.append((line, account))
            total += net
            continue
        line.save(update_fields=["pay_state", "pay_note", "updated_at"])
    if not payable:
        return None
    clearing = resolve_mapped_account(
        entity, AccountMappingKey.GATEWAY_CLEARING, label="gateway clearing")
    label = held._branch_label(entity.tenant, branch_id)
    currency = run.currency or _entity_currency(entity)
    batch = PayoutBatch.objects.create(
        entity=entity, branch_id=branch_id, provider=provider, purpose=PayoutPurpose.PAYROLL,
        reference=_new_reference(entity), status=PayoutBatchStatus.DRAFT,
        title=f"Payroll {run.document_number or run.pk}: {label}"[:200],
        narration=f"Net pay, {run.period_label or run.document_number or ''}".strip()[:255],
        total_amount=total, item_count=len(payable), currency=currency, source_account=clearing,
        created_by=actor_user,
        metadata={"payroll_run_id": run.pk, "branch_id": branch_id, "waiting_for_funds": False},
    )
    for line, account in payable:
        payout = PayoutInstruction.objects.create(
            entity=entity, branch_id=branch_id, batch=batch, provider=provider,
            reference=_new_reference(entity), amount=int(line.net_amount), currency=currency,
            beneficiary_name=(account.resolved_account_name or line.employee_name or "Staff")[:200],
            beneficiary_account_number=account.account_number,
            beneficiary_bank_code=account.bank_code, source_account=clearing,
            narration=batch.narration, status=PayoutStatus.PENDING, created_by=actor_user,
            metadata={"payroll_run_id": run.pk, "payroll_line_id": line.pk,
                      "staff_bank_account_id": account.pk, "wht_amount": 0},
        )
        line.pay_state, line.pay_note = State.SENT, ""
        line.payout_reference = payout.reference
        line.save(update_fields=["pay_state", "pay_note", "payout_reference", "updated_at"])
    audit.record(
        action=PaymentAuditAction.PAYOUT_BATCH_CREATED, entity=entity, provider=provider,
        reference=batch.reference, actor_user=actor_user,
        message=f"Prepared payroll batch of {len(payable)} staff for {label}.",
        metadata={"payroll_run_id": run.pk, "branch_id": branch_id, "total": total},
    )
    return batch


# --------------------------------------------------------------------------- #
# Sending                                                                      #
# --------------------------------------------------------------------------- #

def enqueue_dispatch(batch_ids, actor_user_id=None) -> None:
    """Hand payroll batches to a worker; a broker problem never loses them (the sweep finds them)."""
    from .tasks import dispatch_payroll_batch_task

    for batch_id in batch_ids:
        try:
            dispatch_payroll_batch_task.delay(batch_id, actor_user_id)
        except Exception:  # noqa: BLE001 - the batch is durable and the sweep re-dispatches it.
            logger.exception("Could not enqueue dispatch for payroll batch %s.", batch_id)


def dispatch_payroll_batch(batch_id, actor_user_id=None):
    """Send one branch's payroll batch, or leave it waiting when the branch cannot cover it.

    Runs outside any transaction (a transfer cannot be rolled back). The whole
    batch is checked against the branch's held balance first, transfer fees
    included: a branch holding less than all of it sends nothing and waits, so a
    payroll is never half paid by the arithmetic of arrival. Each line then
    claims itself under the balance's lock (:func:`vs_payments.services._dispatch_transfer`),
    so a concurrent dispatch or a sweep cannot send a line twice. A line the
    provider cleanly rejects reads FAILED; one whose outcome is unknown stays
    sent for the recovery sweep to settle.
    """
    from django.contrib.auth import get_user_model

    from vs_finance.constants import PayrollLinePayState as State

    from . import services
    from .providers.registry import get_provider

    services._assert_no_open_transaction("Dispatching a payroll batch to the provider")
    batch = PayoutBatch.objects.select_related("entity").filter(
        pk=batch_id, purpose=PayoutPurpose.PAYROLL).first()
    if batch is None:
        return None
    assert_run_authorises(batch)
    assert_route_allows(batch.entity)
    actor = get_user_model().objects.filter(pk=actor_user_id).first() if actor_user_id else None
    pending = list(batch.instructions.filter(status=PayoutStatus.PENDING).order_by("pk"))
    if not pending:
        return batch
    client = get_provider(batch.provider)
    needed = sum(int(p.amount) + int(client.transfer_fee(int(p.amount)) or 0) for p in pending)
    own_books = held.is_platform_entity(batch.entity)
    try:
        if own_books:
            available = held.own_share_available(
                client, getattr(batch.currency, "code", None) or "NGN")
        else:
            available = held.held_balance(held.held_branch_id(batch.entity, batch.branch_id))
    except Exception:  # noqa: BLE001 - funds are never assumed; wait and ask again.
        available = None
    if available is None or available < needed:
        if available is None:
            holds = "The platform's own share of the provider balance could not be confirmed"
        elif own_books:
            holds = f"The platform's own share of the provider balance is {held._naira(max(available, 0))}"
        else:
            holds = f"The branch holds {held._naira(max(available, 0))}"
        message = (f"{holds}, and this payroll needs {held._naira(needed)} including transfer "
                   f"fees. It is sent when enough money has cleared.")
        for payout in pending:
            _set_line(payout, pay_state=State.WAITING_FOR_FUNDS, pay_note=message[:255])
        _set_batch_waiting(batch.pk, True)
        return batch
    _set_batch_waiting(batch.pk, False)
    for payout in pending:
        _set_line(payout, pay_state=State.SENT, pay_note="")
    for payout in pending:
        try:
            services._dispatch_transfer(
                payout, batch=batch, approved_instance=None, client=client,
                metadata=payout.metadata or {}, actor_user=actor)
        except held.InsufficientHeldFundsError:
            break  # Another payment spent the money first; the rest wait.
        except PayrollDestinationChanged as exc:
            _abandon_payout(payout, str(getattr(exc, "message", exc)), State.NEEDS_BANK_DETAILS)
        except FinanceError:
            payout.refresh_from_db()
            if payout.status == PayoutStatus.FAILED:
                line_failed(payout, payout.failure_reason)
    with transaction.atomic():
        batch = PayoutBatch.objects.select_for_update().get(pk=batch.pk)
        batch.submitted_at = batch.submitted_at or timezone.now()
        services._recompute_batch_status(batch)
    return batch


def _abandon_payout(payout, reason, line_state) -> None:
    """Fail an unsent payroll instruction whose line can no longer be paid as prepared."""
    from . import services

    with transaction.atomic():
        row = PayoutInstruction.objects.select_for_update().get(pk=payout.pk)
        if row.status == PayoutStatus.PENDING:
            row.status = PayoutStatus.FAILED
            row.failure_reason = str(reason)[:255]
            row.save(update_fields=["status", "failure_reason", "updated_at"])
        _set_line(row, pay_state=line_state, pay_note=str(reason)[:255])
        services._recompute_batch_status(PayoutBatch.objects.select_for_update().get(pk=row.batch_id))


def sweep_payroll_batches() -> dict:
    """Send payroll batches that still hold unsent instructions: waiting for funds, or a lost hand-off.

    A batch whose payroll run can no longer authorise it (cancelled, or the
    tenant stopped paying through the provider) has its unsent instructions
    failed rather than retried for ever.
    """
    dispatched = abandoned = failures = 0
    batches = (PayoutBatch.objects.filter(
        purpose=PayoutPurpose.PAYROLL, instructions__status=PayoutStatus.PENDING)
        .distinct().order_by("pk"))
    for batch in batches:
        try:
            dispatch_payroll_batch(batch.pk)
            dispatched += 1
        except PayoutApprovalRequiredError as exc:
            for payout in batch.instructions.filter(status=PayoutStatus.PENDING):
                _abandon_payout(payout, str(getattr(exc, "message", exc)), "")
            abandoned += 1
        except Exception:  # noqa: BLE001 - one batch must not stop the rest.
            logger.exception("sweep_payroll_batches: batch %s could not be dispatched.", batch.pk)
            failures += 1
    return {"dispatched": dispatched, "abandoned": abandoned, "failures": failures}
