"""Inter-branch transfers: the only records that touch two branches.

Every entry in the books belongs to one branch. When money, a customer's open
balance, a share of a cost or goods pass between two branches, each branch books
its own side in its own journal and the two journals are linked through one
:class:`~vs_finance.models.InterBranchTransfer`. The rule "one entry, one branch"
therefore never has an exception.

One well-known account, the inter-branch balances account
(:data:`~vs_finance.constants.AccountMappingKey.INTER_BRANCH`), carries every
balance between branches. Each of its lines names its counterparty branch
(:attr:`~vs_finance.models.JournalLine.counterparty_branch`), which is what the
pair balances and the register read: on Ikeja's journal, ``Dr inter-branch
[Lekki]`` means Lekki owes Ikeja. Across every branch the account nets to zero,
because every transfer is booked on both sides with the same amount.

The kinds, and the two journals each posts (sending branch first):

* **Cash** (``A`` lends ``B``): ``Dr inter-branch [B], Cr A's bank`` and
  ``Dr B's bank, Cr inter-branch [A]``. ``B`` may request it; ``A`` approves and
  sends it through its own workflow route; ``B`` confirms arrival. A repayment is
  a cash transfer the other way.
* **Forwarded receipt** (``A`` received ``B``'s customer's money): ``Dr held for
  other branches [B], Cr A's bank``, and at ``B`` a receipt against the customer
  in ``B``'s bank, which settles ``B``'s invoice. Nobody owes anybody afterwards.
* **Receivable** (a customer's open balance moves from ``A`` to ``B``):
  ``Dr inter-branch [B], Cr receivable`` and ``Dr receivable, Cr inter-branch
  [A]``. ``B`` collects it, and owes ``A`` the amount.
* **Recharge** (``A`` paid a cost partly ``B``'s): ``Dr inter-branch [B], Cr
  expense`` and ``Dr expense, Cr inter-branch [A]``.
* **Goods** (``A``'s store issues stock to ``B``'s): ``Dr inter-branch [B], Cr
  inventory`` and ``Dr inventory, Cr inter-branch [A]``, at moving-average cost.
* **Income given back** (a credit note or concession at ``A`` cancels a moved
  bill's income ``B`` booked): ``Dr inter-branch [B]`` as a line of the
  adjusting document's own journal, and ``Dr deferred income, revenue or
  allowance and output tax, Cr inter-branch [A]`` at ``B``
  (:func:`book_income_given_back`).
* **Shared bank split** (``A``'s book balance on a shared bank account was more
  than the share it agreed to take, ``B``'s less): ``Dr inter-branch [B], Cr
  shared bank`` and ``Dr shared bank, Cr inter-branch [A]``
  (:func:`book_bank_split_difference`). ``B`` owes ``A`` the difference.

Both branches must be open on the transfer's date (:func:`ensure_branches_open`).
A tenant with one branch has no other branch to transfer to, so every service
here refuses there (:func:`require_several_branches`) and nothing touches the
inter-branch accounts.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from vs_config.clock import branch_today

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    AccountType,
    DocumentStatus,
    FinanceAuditAction,
    InterBranchLegRole,
    InterBranchTransferKind,
    JournalSource,
    PaymentPlanStatus,
    RechargeBasis,
    SharedCostTreatment,
)
from .exceptions import FinanceError, InterBranchError, InterBranchUnavailableError, PostingError
from .money import format_naira
from .posting import ensure_period_open, post_journal, resolve_period, reverse_journal

#: Kinds that move money between two bank accounts.
MONEY_KINDS = (InterBranchTransferKind.CASH, InterBranchTransferKind.FORWARDED_RECEIPT)

#: Basis points in 100%.
WHOLE_BPS = 10000


# --------------------------------------------------------------------------- #
# Shared guards                                                               #
# --------------------------------------------------------------------------- #

def require_several_branches(entity) -> None:
    """Refuse when ``entity``'s tenant owns one branch: there is nobody to transfer to.

    Counted over every branch the tenant owns, whatever its status, as every
    branch rule counts (:func:`vs_rbac.scoping.only_branch_id_or_several`).
    """
    from vs_rbac.scoping import only_branch_id_or_several

    if only_branch_id_or_several(entity.tenant) is not None:
        raise InterBranchUnavailableError(
            "There is only one branch, so there is no other branch to move money, "
            "goods or balances to.",
        )


def tenant_branch(entity, ref, *, field="branch"):
    """The branch ``ref`` (a Branch or its id) names inside ``entity``'s tenant, or a refusal."""
    from vs_tenants.models import Branch

    branch_id = getattr(ref, "pk", ref)
    branch = Branch.all_objects.filter(pk=branch_id, tenant_id=entity.tenant_id).first() \
        if branch_id is not None else None
    if branch is None:
        raise InterBranchError("No such branch in these books.", field=field)
    return branch


def ensure_branches_open(entity, on_date, *branches) -> None:
    """Refuse unless both branches of a transfer can post on ``on_date``.

    The books close by month for the tenant, so both branches are open exactly
    when the tenant's period covering the date accepts a posting. This is the
    one place a transfer asks, so a per-branch close answers here for both
    branches at once.
    """
    period = resolve_period(entity, on_date)
    for branch in branches:
        ensure_period_open(period, branch=branch)


def inter_branch_account(entity):
    """The account every inter-branch balance is booked on."""
    return resolve_mapped_account(
        entity, AccountMappingKey.INTER_BRANCH, label="inter-branch balances",
    )


def held_for_account(entity):
    """The liability money held for another branch is booked on."""
    return resolve_mapped_account(
        entity, AccountMappingKey.HELD_FOR_OTHER_BRANCHES, label="held for other branches",
    )


def _same_branch_account(bank, branch_id, *, side):
    """Refuse ``bank`` unless it is a live account of exactly ``branch_id``.

    No exception for an account not yet given a branch: a transfer runs only at a
    tenant with several branches, where such an account belongs to none.
    """
    if bank is None:
        raise InterBranchError(f"Name the {side} branch's bank account.", field=f"{side}_bank_account")
    if branch_id is None or bank.branch_id is None:
        raise InterBranchError(
            f"{bank.name} has not been given a branch, so no branch can move money "
            f"through it. Give it its branch first.",
            field=f"{side}_bank_account",
        )
    if not bank.is_active:
        raise InterBranchError(f"Bank account {bank.name} is closed.", field=f"{side}_bank_account")
    if bank.branch_id != branch_id:
        raise InterBranchError(
            f"{bank.name} is not an account of the {side} branch. Each branch sends and "
            f"receives through its own accounts.",
            field=f"{side}_bank_account",
        )


def _audit_both(transfer, action, message, *, actor_user=None, **metadata):
    """Write ``action`` once under each branch, so each branch's readers find it."""
    for branch_id in (transfer.branch_id, transfer.to_branch_id):
        record(
            entity=transfer.entity, action=action, actor_user=actor_user, target=transfer,
            branch=branch_id, message=message, amount=int(transfer.amount),
            kind=transfer.kind, from_branch_id=transfer.branch_id,
            to_branch_id=transfer.to_branch_id, **metadata,
        )


def _branch_names(transfer):
    return transfer.branch.name, transfer.to_branch.name


def _post_leg(transfer, *, role, branch_id, counterparty_id, lines, source, narration,
              bank_account=None, actor_user=None):
    """Post one branch's journal for ``transfer`` and record it as that branch's leg.

    ``lines`` are ``(account, debit, credit, counterparty_branch_id)``, with an
    optional fifth item, the cost centre id; the counterparty is set only on the
    inter-branch and held-for lines.
    """
    from .models import InterBranchTransferLeg, JournalEntry, JournalLine

    entry = JournalEntry.objects.create(
        entity=transfer.entity, branch_id=branch_id, date=transfer.transfer_date,
        period=resolve_period(transfer.entity, transfer.transfer_date),
        source=source, narration=narration[:255],
        reference=transfer.document_number, created_by=actor_user,
    )
    for number, (account, debit, credit, counterparty, *rest) in enumerate(lines, start=1):
        JournalLine.objects.create(
            entry=entry, account=account, debit=debit, credit=credit,
            counterparty_branch_id=counterparty, cost_center_id=rest[0] if rest else None,
            description=narration[:255], line_no=number,
        )
    post_journal(entry, actor_user=actor_user)
    InterBranchTransferLeg.objects.create(
        transfer=transfer, role=role, branch_id=branch_id,
        counterparty_branch_id=counterparty_id, bank_account=bank_account, journal=entry,
    )
    return entry


def _post_balance_pair(transfer, *, sending_account, receiving_account, source, actor_user=None):
    """Post the two journals of a kind that moves a balance and no money.

    The sending branch credits ``sending_account`` and is owed; the receiving
    branch debits ``receiving_account`` and owes.
    """
    ib = inter_branch_account(transfer.entity)
    sender, receiver = _branch_names(transfer)
    amount = int(transfer.amount)
    _post_leg(
        transfer, role=InterBranchLegRole.SENDING, branch_id=transfer.branch_id,
        counterparty_id=transfer.to_branch_id, source=source, actor_user=actor_user,
        narration=f"{transfer.get_kind_display()} to {receiver}: {transfer.purpose}",
        lines=[(ib, amount, 0, transfer.to_branch_id), (sending_account, 0, amount, None)],
    )
    _post_leg(
        transfer, role=InterBranchLegRole.RECEIVING, branch_id=transfer.to_branch_id,
        counterparty_id=transfer.branch_id, source=source, actor_user=actor_user,
        narration=f"{transfer.get_kind_display()} from {sender}: {transfer.purpose}",
        lines=[(receiving_account, amount, 0, None), (ib, 0, amount, transfer.branch_id)],
    )


def _new_transfer(entity, *, kind, from_branch_id, to_branch_id, amount, transfer_date,
                  purpose, actor_user=None, **fields):
    """Create a POSTED-bound transfer row of a kind that books at once."""
    from .models import InterBranchTransfer

    if from_branch_id == to_branch_id:
        raise InterBranchError("A transfer needs two different branches.")
    if int(amount or 0) <= 0:
        raise InterBranchError("A transfer must move a positive amount.")
    return InterBranchTransfer.objects.create(
        entity=entity, kind=kind, branch_id=from_branch_id, to_branch_id=to_branch_id,
        amount=int(amount), transfer_date=transfer_date, purpose=(purpose or "")[:255],
        created_by=actor_user, sent_by=actor_user, sent_at=timezone.now(), **fields,
    )


def _finish(transfer, fields=("status",)):
    transfer.status = DocumentStatus.POSTED
    transfer.save(update_fields=[*fields, "updated_at"])


# --------------------------------------------------------------------------- #
# Cash transfers and forwarded receipts                                       #
# --------------------------------------------------------------------------- #

def request_cash_transfer(entity, *, from_branch, to_branch, amount, transfer_date, purpose,
                          repay_by=None, to_bank_account=None, actor_user=None):
    """Record the receiving branch's request for money from another branch.

    Nothing is booked. The request waits, as a DRAFT naming who asked, for the
    sending branch to send it (:func:`prepare_to_send`) or decline it.
    """
    from .models import InterBranchTransfer

    require_several_branches(entity)
    from_branch = tenant_branch(entity, from_branch, field="from_branch")
    to_branch = tenant_branch(entity, to_branch, field="to_branch")
    if from_branch.pk == to_branch.pk:
        raise InterBranchError("Ask another branch; a branch cannot lend to itself.")
    if int(amount or 0) <= 0:
        raise InterBranchError("Ask for a positive amount.", field="amount")
    if to_bank_account is not None:
        _same_branch_account(to_bank_account, to_branch.pk, side="receiving")
    with transaction.atomic():
        transfer = InterBranchTransfer.objects.create(
            entity=entity, kind=InterBranchTransferKind.CASH, branch=from_branch,
            to_branch=to_branch, amount=int(amount), transfer_date=transfer_date,
            purpose=(purpose or "")[:255], repay_by=repay_by, to_bank_account=to_bank_account,
            requested_by=actor_user, requested_at=timezone.now(), created_by=actor_user,
        )
        _audit_both(
            transfer, FinanceAuditAction.INTER_BRANCH_REQUESTED,
            f"{to_branch.name} asked {from_branch.name} for {format_naira(transfer.amount)}: "
            f"{transfer.purpose}",
            actor_user=actor_user,
        )
    return transfer


def default_receiving_account(entity, branch_id):
    """The account money sent to ``branch_id`` lands in when the sender names none.

    The branch's collection account, else its only live account. ``None`` when it
    has several and none is the collection account: the sender must then say which.
    """
    from .models import BankAccount

    live = BankAccount.objects.filter(entity=entity, branch_id=branch_id, is_active=True)
    primary = live.filter(is_primary_collection=True).first()
    if primary is not None:
        return primary
    accounts = list(live[:2])
    return accounts[0] if len(accounts) == 1 else None


def validate_money_transfer(transfer) -> None:
    """Refuse a cash transfer or forwarded receipt that could never post.

    Run before it is saved, before it is routed for approval and again when it
    posts, so an account moved to another branch or closed while the transfer
    waited cannot carry the money with it.
    """
    from .models import InterBranchTransfer

    require_several_branches(transfer.entity)
    if transfer.kind not in MONEY_KINDS:
        raise InterBranchError(f"A {transfer.get_kind_display().lower()} transfer moves no money.")
    if transfer.branch_id is None or transfer.branch_id == transfer.to_branch_id:
        raise InterBranchError("A transfer needs two different branches.")
    if int(transfer.amount or 0) <= 0:
        raise InterBranchError("A transfer must move a positive amount.", field="amount")
    for account in (transfer.from_bank_account, transfer.to_bank_account):
        if account is not None and account.entity_id != transfer.entity_id:
            raise InterBranchError(f"Bank account {account.name} belongs to other books.")
    _same_branch_account(transfer.from_bank_account, transfer.branch_id, side="sending")
    _same_branch_account(transfer.to_bank_account, transfer.to_branch_id, side="receiving")
    if transfer.kind == InterBranchTransferKind.FORWARDED_RECEIPT:
        held = transfer.held_receipt
        if held is None or held.status != DocumentStatus.POSTED:
            raise InterBranchError("Only a posted receipt held for another branch can be forwarded.")
        if (held.branch_id, held.for_branch_id) != (transfer.branch_id, transfer.to_branch_id):
            raise InterBranchError(
                f"Receipt {held.document_number} is held for {held.for_branch.name}; "
                f"forward it from the branch that received it to that branch.",
            )
        if int(transfer.amount) != int(held.amount):
            raise InterBranchError("A held receipt is forwarded whole.", field="amount")
        live = InterBranchTransfer.objects.filter(
            held_receipt=held,
            status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED, DocumentStatus.POSTED),
        ).exclude(pk=transfer.pk)
        if live.exists():
            raise InterBranchError(f"Receipt {held.document_number} is already being forwarded.")


def post_inter_branch_transfer(transfer, *, actor_user=None):
    """Send a draft or approved cash transfer or forwarded receipt, recording refusals."""
    try:
        return _post_money_transfer_atomic(transfer, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=transfer.entity, action=FinanceAuditAction.INTER_BRANCH_SENT,
            exc=exc, actor_user=actor_user, target=transfer,
        )
        raise


@transaction.atomic
def _post_money_transfer_atomic(transfer, *, actor_user=None):
    """Book both sides of a money transfer and mark it POSTED (sent).

    A cash transfer posts the sending branch's ``Dr inter-branch [receiving], Cr
    bank`` and the receiving branch's ``Dr bank, Cr inter-branch [sending]``. A
    forwarded receipt posts the sending branch's ``Dr held for other branches
    [receiving], Cr bank`` and, at the receiving branch, a receipt against the
    customer in the receiving bank, which settles the customer's open invoices
    of that branch oldest first; whatever they do not take waits as the
    customer's credit there.

    Both sides post on ``transfer_date``, so each bank's ledger shows the
    movement on the same day and each side reaches its own bank reconciliation.
    """
    from .models import InterBranchTransfer, Payment
    from .receivables import post_payment

    transfer = InterBranchTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status not in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):
        raise PostingError(
            f"Transfer {transfer.document_number or transfer.pk} is '{transfer.status}'; "
            f"only a draft or approved one can be sent.",
        )
    validate_money_transfer(transfer)
    ensure_branches_open(
        transfer.entity, transfer.transfer_date, transfer.branch_id, transfer.to_branch_id,
    )
    sender, receiver = _branch_names(transfer)
    amount = int(transfer.amount)
    source_bank, target_bank = transfer.from_bank_account, transfer.to_bank_account

    if transfer.kind == InterBranchTransferKind.CASH:
        ib = inter_branch_account(transfer.entity)
        _post_leg(
            transfer, role=InterBranchLegRole.SENDING, branch_id=transfer.branch_id,
            counterparty_id=transfer.to_branch_id, source=JournalSource.BANK,
            bank_account=source_bank, actor_user=actor_user,
            narration=f"Sent to {receiver}: {transfer.purpose}",
            lines=[(ib, amount, 0, transfer.to_branch_id), (source_bank.gl_account, 0, amount, None)],
        )
        _post_leg(
            transfer, role=InterBranchLegRole.RECEIVING, branch_id=transfer.to_branch_id,
            counterparty_id=transfer.branch_id, source=JournalSource.BANK,
            bank_account=target_bank, actor_user=actor_user,
            narration=f"Received from {sender}: {transfer.purpose}",
            lines=[(target_bank.gl_account, amount, 0, None), (ib, 0, amount, transfer.branch_id)],
        )
    else:
        from .models import InterBranchTransferLeg

        held = transfer.held_receipt
        _post_leg(
            transfer, role=InterBranchLegRole.SENDING, branch_id=transfer.branch_id,
            counterparty_id=transfer.to_branch_id, source=JournalSource.BANK,
            bank_account=source_bank, actor_user=actor_user,
            narration=f"Forwarded {held.document_number} to {receiver}: {transfer.purpose}",
            lines=[
                (held_for_account(transfer.entity), amount, 0, transfer.to_branch_id),
                (source_bank.gl_account, 0, amount, None),
            ],
        )
        receipt = Payment.objects.create(
            entity=transfer.entity, branch_id=transfer.to_branch_id, customer=held.customer,
            payment_date=transfer.transfer_date, method=held.method, amount=amount,
            deposit_account=target_bank.gl_account, reference=held.reference or held.document_number,
            narration=f"Forwarded from {sender} ({held.document_number})"[:255],
            created_by=actor_user,
        )
        post_payment(receipt, actor_user=actor_user, auto_allocate=True)
        InterBranchTransferLeg.objects.create(
            transfer=transfer, role=InterBranchLegRole.RECEIVING, branch_id=transfer.to_branch_id,
            counterparty_branch_id=transfer.branch_id, bank_account=target_bank, journal=None,
        )
        transfer.receipt = receipt

    transfer.status = DocumentStatus.POSTED
    transfer.sent_by = actor_user
    transfer.sent_at = timezone.now()
    transfer.save(update_fields=["status", "sent_by", "sent_at", "receipt", "updated_at"])
    _audit_both(
        transfer, FinanceAuditAction.INTER_BRANCH_SENT,
        f"{sender} sent {format_naira(amount)} to {receiver}: {transfer.purpose}",
        actor_user=actor_user, from_account_id=source_bank.pk, to_account_id=target_bank.pk,
        receipt_id=transfer.receipt_id,
    )
    return transfer


@transaction.atomic
def decline_request(transfer, *, reason="", actor_user=None):
    """The sending branch declines a request it will not meet. Nothing was booked."""
    from .models import InterBranchTransfer

    transfer = InterBranchTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != DocumentStatus.DRAFT or transfer.requested_by_id is None:
        raise InterBranchError("Only a request still waiting to be sent can be declined.")
    transfer.status = DocumentStatus.CANCELLED
    transfer.declined_by = actor_user
    transfer.declined_at = timezone.now()
    transfer.decline_reason = (reason or "")[:255]
    transfer.save(update_fields=["status", "declined_by", "declined_at", "decline_reason", "updated_at"])
    _audit_both(
        transfer, FinanceAuditAction.INTER_BRANCH_DECLINED,
        f"{transfer.branch.name} declined {transfer.to_branch.name}'s request for "
        f"{format_naira(transfer.amount)}" + (f": {transfer.decline_reason}" if transfer.decline_reason else "."),
        actor_user=actor_user,
    )
    return transfer


@transaction.atomic
def confirm_arrival(transfer, *, arrival_date=None, actor_user=None):
    """The receiving branch confirms the money reached its bank.

    The books already hold both sides; confirmation is the receiving branch's
    word that the money arrived, which the bank reconciliation then proves.
    """
    from .models import InterBranchTransfer

    transfer = InterBranchTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.kind not in MONEY_KINDS:
        raise InterBranchError("Only a transfer of money has an arrival to confirm.")
    if transfer.status != DocumentStatus.POSTED:
        raise InterBranchError("Only a sent transfer can be confirmed as arrived.")
    if transfer.received_at is not None:
        raise InterBranchError(f"Transfer {transfer.document_number} is already confirmed.")
    arrived = arrival_date or branch_today(transfer.entity.tenant, transfer.to_branch_id)
    if arrived < transfer.transfer_date:
        raise InterBranchError("Money cannot arrive before it was sent.", field="arrival_date")
    transfer.received_by = actor_user
    transfer.received_at = timezone.now()
    transfer.arrival_date = arrived
    transfer.save(update_fields=["received_by", "received_at", "arrival_date", "updated_at"])
    _audit_both(
        transfer, FinanceAuditAction.INTER_BRANCH_CONFIRMED,
        f"{transfer.to_branch.name} confirmed {format_naira(transfer.amount)} from "
        f"{transfer.branch.name} arrived.",
        actor_user=actor_user, arrival_date=str(arrived),
    )
    return transfer


# --------------------------------------------------------------------------- #
# Money held for another branch                                               #
# --------------------------------------------------------------------------- #

def validate_held_receipt(held) -> None:
    """Refuse a held receipt that could never post.

    The receiving bank account must be the document's own branch's; the customer
    must belong to the branch the money is held for, or be shared by every
    branch.
    """
    require_several_branches(held.entity)
    if int(held.amount or 0) <= 0:
        raise InterBranchError("A receipt must be for a positive amount.", field="amount")
    if held.for_branch_id == held.branch_id:
        raise InterBranchError(
            "This money belongs to the branch that received it; record an ordinary receipt.",
            field="for_branch",
        )
    _same_branch_account(held.bank_account, held.branch_id, side="receiving")
    customer = held.customer
    if customer.entity_id != held.entity_id:
        raise InterBranchError("The customer belongs to other books.", field="customer")
    if customer.branch_id not in (None, held.for_branch_id):
        raise InterBranchError(
            f"{customer.name} is {customer.branch.name}'s customer, not "
            f"{held.for_branch.name}'s.",
            field="customer",
        )


@transaction.atomic
def post_held_receipt(held, *, actor_user=None):
    """Book money received for another branch: ``Dr bank, Cr held for other branches [for]``."""
    from .models import HeldForBranchReceipt, JournalEntry, JournalLine

    held = HeldForBranchReceipt.objects.select_for_update().get(pk=held.pk)
    if held.status != DocumentStatus.DRAFT:
        raise PostingError(f"Receipt {held.document_number} is '{held.status}' and cannot be posted.")
    validate_held_receipt(held)
    narration = (
        held.narration or f"Received for {held.for_branch.name}: {held.customer.name}"
    )[:255]
    entry = JournalEntry.objects.create(
        entity=held.entity, branch_id=held.branch_id, date=held.receipt_date,
        period=resolve_period(held.entity, held.receipt_date), source=JournalSource.BANK,
        narration=narration, reference=held.reference or held.document_number,
        created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=held.bank_account.gl_account, debit=held.amount, credit=0,
        description=narration, line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=held_for_account(held.entity), debit=0, credit=held.amount,
        counterparty_branch_id=held.for_branch_id, description=narration, line_no=2,
    )
    post_journal(entry, actor_user=actor_user)
    held.journal = entry
    held.status = DocumentStatus.POSTED
    held.save(update_fields=["journal", "status", "updated_at"])
    for branch_id in (held.branch_id, held.for_branch_id):
        record(
            entity=held.entity, action=FinanceAuditAction.HELD_RECEIPT_POSTED,
            actor_user=actor_user, target=held, branch=branch_id,
            message=(
                f"{held.branch.name} received {format_naira(held.amount)} from "
                f"{held.customer.name} that belongs to {held.for_branch.name}."
            ),
            amount=int(held.amount), journal_id=entry.pk, for_branch_id=held.for_branch_id,
        )
    return held


@transaction.atomic
def void_held_receipt(held, *, actor_user=None, date=None, payer_payment=None):
    """Reverse a held receipt that was never forwarded and that no statement has matched.

    One that is a customer's share of a payer's payment is voided only with that
    payment (``payer_payment``), which voids every share together.
    """
    from .banking import journal_is_reconciled
    from .models import HeldForBranchReceipt, InterBranchTransfer
    from .payer_payments import payer_payment_refusal

    held = HeldForBranchReceipt.objects.select_for_update().get(pk=held.pk)
    if held.status != DocumentStatus.POSTED or held.journal_id is None:
        raise InterBranchError("Only a posted held receipt can be voided.")
    split_from = payer_payment_refusal("held_receipt", held, payer_payment)
    if split_from is not None:
        raise InterBranchError(split_from)
    forward = InterBranchTransfer.objects.filter(
        held_receipt=held,
        status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED, DocumentStatus.POSTED),
    ).first()
    if forward is not None:
        raise InterBranchError(
            f"Receipt {held.document_number} is forwarded by {forward.document_number}; "
            f"void that transfer first.",
        )
    if journal_is_reconciled(held.journal_id):
        raise InterBranchError(
            f"Receipt {held.document_number} is matched to a bank statement line. "
            f"Unmatch it on the reconciliation first.",
        )
    reversal = reverse_journal(held.journal, actor_user=actor_user, date=date, document_owner=held)
    held.status = DocumentStatus.REVERSED
    held.save(update_fields=["status", "updated_at"])
    for branch_id in (held.branch_id, held.for_branch_id):
        record(
            entity=held.entity, action=FinanceAuditAction.HELD_RECEIPT_VOIDED,
            actor_user=actor_user, target=held, branch=branch_id,
            message=f"Voided held receipt {held.document_number}.",
            amount=int(held.amount), journal_id=held.journal_id, reversal_id=reversal.pk,
        )
    return held


def forward_held_receipt(held, *, to_bank_account, transfer_date, from_bank_account=None,
                         purpose="", actor_user=None):
    """Build the forwarded-receipt transfer for ``held``, unsaved approval aside.

    Returns a saved DRAFT transfer; the caller routes it for approval or posts it,
    exactly as a cash transfer is sent. It goes from the bank that received the
    money unless ``from_bank_account`` names another account of the same branch.
    """
    from .models import InterBranchTransfer

    transfer = InterBranchTransfer(
        entity=held.entity, kind=InterBranchTransferKind.FORWARDED_RECEIPT,
        branch_id=held.branch_id, to_branch_id=held.for_branch_id, amount=held.amount,
        transfer_date=transfer_date, customer=held.customer, held_receipt=held,
        from_bank_account=from_bank_account or held.bank_account, to_bank_account=to_bank_account,
        purpose=(purpose or f"Forwarding {held.customer.name}'s payment {held.document_number}")[:255],
        reference=held.reference, created_by=actor_user,
    )
    validate_money_transfer(transfer)
    transfer.save()
    return transfer


# --------------------------------------------------------------------------- #
# Voids                                                                       #
# --------------------------------------------------------------------------- #

def void_inter_branch_transfer(transfer, *, actor_user=None, date=None):
    """Void a posted transfer by reversing both sides, recording refusals."""
    try:
        return _void_transfer_atomic(transfer, actor_user=actor_user, date=date)
    except FinanceError as exc:
        record_rejection(
            entity=transfer.entity, action=FinanceAuditAction.INTER_BRANCH_VOIDED,
            exc=exc, actor_user=actor_user, target=transfer,
        )
        raise


@transaction.atomic
def _void_transfer_atomic(transfer, *, actor_user=None, date=None, recharge=None,
                          adjustment_entry=None):
    """Reverse both branches' journals and mark the transfer REVERSED.

    * A cash transfer or forwarded receipt is refused once a bank statement line
      on either side is matched to it: the bank says the money moved, so the match
      is undone first. A forwarded receipt's receiving side is its receipt, which
      is voided with it and gives back what it settled; the held receipt is then
      free to be forwarded again.
    * A receivable move voids its credit receipt first, giving back what that
      settled, and is then refused once any moved invoice or debit note has been
      paid, credited or written off since, or a moved deferred share released:
      those were booked at the new branch against what it took over. Otherwise
      everything goes back to the branch it came from and the drawn credit is
      restored to its receipts and credit notes.
    * A recharge share is voided only with its recharge, and goods only by sending
      them back: the stock has moved, and a reversal would not move it back.
    * A shared bank split's difference is never voided: the split retired the
      shared ledger it moved, so the owing branch repays it with a cash transfer.
    * Income given back is voided only with the credit note or concession that
      gave it back (``adjustment_entry``, from
      :func:`vs_finance.deferred_income.restore_unwinds`): voiding it alone would
      put the income back at the branch that held it while the document still
      took it from the customer.
    """
    from .banking import journal_is_reconciled
    from .models import InterBranchTransfer
    from .voids import _void_payment_atomic

    transfer = InterBranchTransfer.objects.select_for_update().get(pk=transfer.pk)
    if transfer.status != DocumentStatus.POSTED:
        raise InterBranchError(
            f"Only a posted transfer can be voided; {transfer.document_number} is '{transfer.status}'.",
        )
    if transfer.kind == InterBranchTransferKind.GOODS:
        raise InterBranchError(
            f"Transfer {transfer.document_number} moved goods. Send them back with a goods "
            f"transfer the other way instead.",
        )
    if transfer.kind == InterBranchTransferKind.BANK_SPLIT:
        raise InterBranchError(
            f"Transfer {transfer.document_number} carries a difference agreed when a shared "
            f"bank account was split, and that account is retired. Settle it with a cash "
            f"transfer the other way instead.",
        )
    if transfer.kind == InterBranchTransferKind.RECHARGE and (
            recharge is None or recharge.pk != transfer.recharge_id):
        raise InterBranchError(
            f"Transfer {transfer.document_number} is a share of recharge "
            f"{transfer.recharge.document_number}; void the recharge instead.",
        )
    if transfer.kind == InterBranchTransferKind.INCOME_GIVEN_BACK and (
            adjustment_entry is None or adjustment_entry.pk != transfer.adjustment_entry_id):
        raise InterBranchError(
            f"Transfer {transfer.document_number} gives back income taken by "
            f"{transfer.reference or 'an adjusting document'}; void that document instead.",
        )
    legs = list(transfer.legs.select_related("journal").order_by("role"))
    journals = [leg.journal for leg in legs if leg.journal_id]
    if transfer.receipt_id:
        journals.append(transfer.receipt.journal)
    if transfer.kind in MONEY_KINDS and any(journal_is_reconciled(j.pk) for j in journals if j):
        raise InterBranchError(
            f"Transfer {transfer.document_number} is matched to a bank statement line on "
            f"one of its accounts. Unmatch it on that reconciliation first.",
        )
    reversals = []
    if transfer.receipt_id:
        _void_payment_atomic(transfer.receipt, actor_user=actor_user, date=date, forwarded=transfer)
    if transfer.kind == InterBranchTransferKind.RECEIVABLE:
        _ensure_moved_items_untouched(transfer)
    for leg in legs:
        if leg.journal_id:
            reversals.append(reverse_journal(
                leg.journal, actor_user=actor_user, date=date, document_owner=transfer,
            ).pk)
    if transfer.kind == InterBranchTransferKind.RECEIVABLE:
        _return_moved_items(transfer)

    transfer.status = DocumentStatus.REVERSED
    transfer.save(update_fields=["status", "updated_at"])
    _audit_both(
        transfer, FinanceAuditAction.INTER_BRANCH_VOIDED,
        f"Voided {transfer.get_kind_display().lower()} transfer {transfer.document_number} "
        f"between {transfer.branch.name} and {transfer.to_branch.name}.",
        actor_user=actor_user, reversal_ids=reversals,
    )
    return transfer


# --------------------------------------------------------------------------- #
# Receivable moves                                                            #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReceivableMove:
    """What :func:`transfer_open_receivables` moved.

    ``amount`` is the customer's net balance that moved: open invoices and debit
    notes less the unapplied credit that went with them, so it is negative when
    the customer was in credit. ``deferred_amount`` is the income not yet earned
    that moved with the invoices. ``transfer_id`` is the move's record, ``None``
    when nothing at the old branch had to move.
    """

    transfer_id: int | None
    amount: int
    invoice_count: int
    invoice_ids: tuple = ()
    debit_note_count: int = 0
    credit_count: int = 0
    credit_amount: int = 0
    deferred_amount: int = 0


def _move_result(transfer) -> ReceivableMove:
    from .constants import ReceivableMoveItemKind as Kind

    items = list(transfer.moved_items.order_by("kind", "invoice_id", "note_id", "payment_id"))
    invoices = [i for i in items if i.kind == Kind.INVOICE]
    notes = [i for i in items if i.kind == Kind.DEBIT_NOTE]
    credits = [i for i in items if i.kind in (Kind.RECEIPT_CREDIT, Kind.NOTE_CREDIT)]
    owed = sum(int(i.amount) for i in invoices + notes)
    credit = sum(int(i.amount) for i in credits)
    return ReceivableMove(
        transfer_id=transfer.pk, amount=owed - credit, invoice_count=len(invoices),
        invoice_ids=tuple(sorted(i.invoice_id for i in invoices)),
        debit_note_count=len(notes), credit_count=len(credits), credit_amount=credit,
        deferred_amount=sum(int(i.deferred_amount) for i in invoices),
    )


def _unearned_entries(invoice_ids, branch_id, after):
    """The deferred-income shares of these invoices not yet earned by ``after``.

    A share recognised on or before ``after`` was earned at the old branch, even
    if its release has not run yet, so it stays there.
    """
    from .constants import DeferredIncomeStatus
    from .models import DeferredIncomeEntry

    return (
        DeferredIncomeEntry.objects
        .filter(invoice_id__in=invoice_ids, branch_id=branch_id,
                status=DeferredIncomeStatus.PENDING, recognition_date__gt=after)
        .order_by("pk")
    )


def transfer_open_receivables(customer, from_branch, to_branch, actor, *, move_date=None,
                              move_key="", purpose=""):
    """Move ``customer``'s whole position at ``from_branch`` to ``to_branch``.

    What moves, so the new branch holds everything and the old branch keeps only
    the revenue it has already earned:

    * every posted invoice still owing, and every posted invoice with income not
      yet earned (a deferred share recognised after ``move_date``);
    * every posted debit note still owing;
    * the customer's unapplied credit at the old branch (receipts and credit
      notes not yet spent, less what a refund awaiting approval has reserved),
      drawn from its receipts and credit notes oldest first;
    * the income not yet earned on the moved invoices: their pending
      deferred-income shares recognised after ``move_date`` are given the new
      branch, so each later release recognises the revenue there. Shares already
      released, and shares recognised by ``move_date`` but not yet released,
      stay at the old branch, which earned them.

    **The re-branching rule.** A moved invoice or debit note is given
    ``to_branch`` in its own branch column, so the new branch's bursar sees and
    chases it under :func:`vs_rbac.scoping.transaction_branch_q`, and every
    document raised on it afterwards (a receipt, a credit note, a write-off)
    takes the new branch; a live payment plan on a moved invoice moves too. Its
    revenue journal stays at ``from_branch``. Credit is not re-branched: its
    source documents stay where the money came in, record the draw in
    ``transferred_amount``, and the credit reappears at the new branch as one
    receipt against the customer, applied to their open bills there when the
    tenant auto-applies credit.

    **The books.** One journal per branch. The old branch credits the receivable
    for the open bills and debits customer credit and deferred income for what
    it hands over; the new branch books the mirror image. The difference is the
    inter-branch balance: the new branch owes the old one for bills it will
    collect, and is owed for credit and unearned income it now answers for.

    Runs inside the caller's transaction and locks what it moves in id order.
    Idempotent per move: a second call finds nothing at ``from_branch`` and
    returns an empty :class:`ReceivableMove`; a ``move_key`` a standing move
    already used returns that move (:func:`_live_move`), even for a retry that
    raced it. A voided move gives its key up: Tunde's move to Lekki is voided,
    his account is put back at Ikeja, his bill voided and the fee run redone,
    and the run's same key then moves his new bill again rather than answering
    with the voided move. The customer's own branch is not touched: whoever
    moves the customer does that.
    """
    entity = customer.entity
    require_several_branches(entity)
    source = tenant_branch(entity, from_branch, field="from_branch")
    target = tenant_branch(entity, to_branch, field="to_branch")
    if source.pk == target.pk:
        raise InterBranchError("The balance is already at that branch.", field="to_branch")
    key = (move_key or "")[:96]
    done = _live_move(entity, key)
    if done is not None:
        return _move_result(done)
    receivable = customer.receivable_account
    if receivable is None:
        raise InterBranchError(f"Customer {customer.code} has no receivable account.")
    on = move_date or branch_today(entity.tenant, target.pk)

    try:
        return _move_open_receivables(
            customer, source, target, actor, on=on, key=key, purpose=purpose, receivable=receivable)
    except IntegrityError:
        done = _live_move(entity, key)
        if done is None:
            raise
        return _move_result(done)


def _live_move(entity, key):
    """The standing (not voided) move ``key`` names, or ``None``. A blank key names none."""
    from .models import InterBranchTransfer

    if not key:
        return None
    return (
        InterBranchTransfer.objects
        .filter(entity=entity, move_key=key).exclude(status=DocumentStatus.REVERSED).first()
    )


def _move_open_receivables(customer, source, target, actor, *, on, key, purpose, receivable):
    """The work of :func:`transfer_open_receivables`, in one savepoint.

    Two retries of one key race safely. The second waits on the first's locks;
    once they are released it finds the first's move and returns it. Where
    nothing it locks overlaps, the live-key unique index refuses its insert, the
    savepoint rolls its writes back, and the caller returns the first's move.
    """
    from .chronology import credit_lots, plan_credit_draw
    from .constants import CREDIT_TRANSFER_METHOD, CreditNoteKind
    from .constants import ReceivableMoveItemKind as Kind
    from .document_settings import resolve_finance_document_settings
    from .models import CreditNote, Invoice, Payment, PaymentPlan, ReceivableTransferItem
    from .receivables import _post_payment_atomic, customer_refund_available_balance

    entity = customer.entity
    with transaction.atomic():
        invoices = list(
            Invoice.objects.select_for_update(of=("self",))
            .filter(entity=entity, customer=customer, branch_id=source.pk,
                    status=DocumentStatus.POSTED)
            .order_by("pk")
        )
        done = _live_move(entity, key)  # A retry that waited on these locks.
        if done is not None:
            return _move_result(done)
        unearned = {}
        for entry in _unearned_entries([i.pk for i in invoices], source.pk, on).select_for_update():
            unearned[entry.invoice_id] = unearned.get(entry.invoice_id, 0) + entry.open_amount
        moving = [i for i in invoices if i.balance_due > 0 or unearned.get(i.pk, 0) > 0]
        notes = [
            n for n in CreditNote.objects.select_for_update(of=("self",))
            .filter(entity=entity, customer=customer, branch_id=source.pk,
                    status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)
            .order_by("pk")
            if n.balance_due > 0
        ]
        # Debit notes leave first, so the credit measured below is not held back for them.
        CreditNote.objects.filter(pk__in=[n.pk for n in notes]).update(
            branch=target, updated_at=timezone.now())
        credit = customer_refund_available_balance(customer, as_of=on, branch=source.pk)
        lots = credit_lots(entity, [customer.pk], as_of=on, branch=source.pk).get(customer.pk, [])
        draws = [(lot, taken) for lot, taken in plan_credit_draw(lots, credit) if taken > 0]
        if not moving and not notes and not draws:
            return ReceivableMove(None, 0, 0, ())

        ensure_branches_open(entity, on, source, target)
        owed = sum(i.balance_due for i in moving) + sum(n.balance_due for n in notes)
        credit_total = sum(taken for _, taken in draws)
        deferred_total = sum(unearned.get(i.pk, 0) for i in moving)
        transfer = _new_transfer(
            entity, kind=InterBranchTransferKind.RECEIVABLE, from_branch_id=source.pk,
            to_branch_id=target.pk, amount=owed + credit_total + deferred_total,
            transfer_date=on, actor_user=actor,
            purpose=purpose or f"Balance of {customer.name} moved to {target.name}",
            customer=customer, move_key=key,
        )
        _post_receivable_pair(
            transfer, receivable=receivable, owed=owed, credit=credit_total,
            deferred=deferred_total, actor_user=actor,
        )

        items = [
            ReceivableTransferItem(transfer=transfer, kind=Kind.INVOICE, invoice=i, from_branch=source,
                                   amount=i.balance_due, deferred_amount=unearned.get(i.pk, 0))
            for i in moving
        ] + [
            ReceivableTransferItem(transfer=transfer, kind=Kind.DEBIT_NOTE, note=n, from_branch=source,
                                   amount=n.balance_due)
            for n in notes
        ]
        for lot, taken in draws:
            if lot.kind == "RECEIPT":
                document = Payment.objects.select_for_update(of=("self",)).get(pk=lot.document_id)
                items.append(ReceivableTransferItem(
                    transfer=transfer, kind=Kind.RECEIPT_CREDIT, payment=document,
                    from_branch=source, amount=taken))
            else:
                document = CreditNote.objects.select_for_update(of=("self",)).get(pk=lot.document_id)
                items.append(ReceivableTransferItem(
                    transfer=transfer, kind=Kind.NOTE_CREDIT, note=document,
                    from_branch=source, amount=taken))
            document.transferred_amount += taken
            document.save(update_fields=["transferred_amount", "updated_at"])
        ReceivableTransferItem.objects.bulk_create(items)

        ids = [i.pk for i in moving]
        _unearned_entries(ids, source.pk, on).update(branch=target, updated_at=timezone.now())
        Invoice.objects.filter(pk__in=ids).update(branch=target, updated_at=timezone.now())
        PaymentPlan.objects.filter(
            invoice_id__in=ids, plan_status__in=(PaymentPlanStatus.DRAFT, PaymentPlanStatus.ACTIVE),
        ).update(branch=target, updated_at=timezone.now())

        if credit_total:
            receipt = Payment.objects.create(
                entity=entity, customer=customer, branch=target, payment_date=on,
                method=CREDIT_TRANSFER_METHOD, amount=credit_total,
                deposit_account=resolve_mapped_account(
                    entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),
                reference=transfer.document_number,
                narration=f"Credit moved from {source.name} ({transfer.document_number})",
                created_by=actor,
            )
            settles = resolve_finance_document_settings(entity).auto_apply_customer_credit
            _post_payment_atomic(receipt, actor_user=actor, auto_allocate=settles)
            transfer.receipt = receipt
        _finish(transfer, fields=("status", "receipt"))
        result = _move_result(transfer)
        _audit_both(
            transfer, FinanceAuditAction.RECEIVABLE_TRANSFERRED,
            f"{customer.name}'s balance moved from {source.name} to {target.name}: "
            f"{format_naira(owed)} owed on {result.invoice_count} invoice(s) and "
            f"{result.debit_note_count} debit note(s), {format_naira(credit_total)} of credit "
            f"and {format_naira(deferred_total)} of income not yet earned.",
            actor_user=actor, customer_id=customer.pk, invoice_ids=list(result.invoice_ids),
            owed=owed, credit=credit_total, deferred=deferred_total, net=result.amount,
        )
    return result


def _post_receivable_pair(transfer, *, receivable, owed, credit, deferred, actor_user=None):
    """Post each branch's journal of a receivable move.

    The old branch: ``Cr receivable`` for the open bills, ``Dr customer credit``
    and ``Dr deferred income`` for what it hands over, and the difference on the
    inter-branch account naming the new branch. The new branch: the mirror image.
    """
    entity = transfer.entity
    ib = inter_branch_account(entity)
    customer_credit = resolve_mapped_account(entity, AccountMappingKey.CUSTOMER_CREDIT)
    deferred_income = resolve_mapped_account(entity, AccountMappingKey.DEFERRED_INCOME)
    net = owed - credit - deferred
    sender, receiver = _branch_names(transfer)

    def lines(sign, counterparty):
        """``sign`` +1 books the sending side; -1 its mirror. A positive amount is a debit."""
        parts = [(receivable, -owed, None), (customer_credit, credit, None),
                 (deferred_income, deferred, None), (ib, net, counterparty)]
        return [
            (account, max(amount * sign, 0), max(-amount * sign, 0), counter)
            for account, amount, counter in parts if amount
        ]

    _post_leg(
        transfer, role=InterBranchLegRole.SENDING, branch_id=transfer.branch_id,
        counterparty_id=transfer.to_branch_id, source=JournalSource.SALES, actor_user=actor_user,
        narration=f"Customer balance moved to {receiver}: {transfer.purpose}",
        lines=lines(1, transfer.to_branch_id),
    )
    _post_leg(
        transfer, role=InterBranchLegRole.RECEIVING, branch_id=transfer.to_branch_id,
        counterparty_id=transfer.branch_id, source=JournalSource.SALES, actor_user=actor_user,
        narration=f"Customer balance moved from {sender}: {transfer.purpose}",
        lines=lines(-1, transfer.branch_id),
    )


def _ensure_moved_items_untouched(transfer) -> None:
    """Refuse to undo a move once anything it moved has been settled or adjusted since.

    Called after the move's own credit receipt is voided, so what that receipt
    settled is already given back and only later documents count.
    """
    from .constants import ReceivableMoveItemKind as Kind
    from .models import CreditNote, Invoice

    def refuse(number):
        raise InterBranchError(
            f"{number} has been paid, credited or changed at {transfer.to_branch.name} since "
            f"it moved, so the move cannot be undone. Move the balance back with another "
            f"move instead.",
        )

    items = list(transfer.moved_items.order_by("pk"))
    invoices = Invoice.objects.select_for_update(of=("self",)).in_bulk(
        [i.invoice_id for i in items if i.invoice_id])
    notes = CreditNote.objects.select_for_update(of=("self",)).in_bulk(
        [i.note_id for i in items if i.kind == Kind.DEBIT_NOTE])
    for item in items:
        if item.kind == Kind.INVOICE:
            invoice = invoices[item.invoice_id]
            still = _unearned_entries([invoice.pk], transfer.to_branch_id, transfer.transfer_date)
            pending = sum(entry.open_amount for entry in still)
            if (invoice.status != DocumentStatus.POSTED or invoice.branch_id != transfer.to_branch_id
                    or invoice.balance_due != int(item.amount) or pending != int(item.deferred_amount)):
                refuse(f"Invoice {invoice.document_number}")
        elif item.kind == Kind.DEBIT_NOTE:
            note = notes[item.note_id]
            if (note.status != DocumentStatus.POSTED or note.branch_id != transfer.to_branch_id
                    or note.balance_due != int(item.amount)):
                refuse(f"Debit note {note.document_number}")


def _return_moved_items(transfer) -> None:
    """Give everything a voided move carried back to the branch it came from."""
    from .constants import ReceivableMoveItemKind as Kind
    from .models import CreditNote, Invoice, Payment, PaymentPlan

    now = timezone.now()
    items = list(transfer.moved_items.order_by("pk"))
    ids = [i.invoice_id for i in items if i.kind == Kind.INVOICE]
    _unearned_entries(ids, transfer.to_branch_id, transfer.transfer_date).update(
        branch_id=transfer.branch_id, updated_at=now)
    Invoice.objects.filter(pk__in=ids).update(branch_id=transfer.branch_id, updated_at=now)
    PaymentPlan.objects.filter(
        invoice_id__in=ids, plan_status__in=(PaymentPlanStatus.DRAFT, PaymentPlanStatus.ACTIVE),
    ).update(branch_id=transfer.branch_id, updated_at=now)
    CreditNote.objects.filter(
        pk__in=[i.note_id for i in items if i.kind == Kind.DEBIT_NOTE],
    ).update(branch_id=transfer.branch_id, updated_at=now)
    for item in items:
        if item.kind == Kind.RECEIPT_CREDIT:
            document = Payment.objects.select_for_update(of=("self",)).get(pk=item.payment_id)
        elif item.kind == Kind.NOTE_CREDIT:
            document = CreditNote.objects.select_for_update(of=("self",)).get(pk=item.note_id)
        else:
            continue
        document.transferred_amount -= int(item.amount)
        document.save(update_fields=["transferred_amount", "updated_at"])


def moved_document_refusal(document):
    """Why ``document`` cannot be voided on its own while a receivable move stands, or ``None``.

    An invoice or debit note carried to another branch keeps its journal at the
    old branch while its receivable sits at the new one; voiding it would take the
    receivable off the old branch a second time. A receipt or credit note whose
    credit a move drew on would hand back credit the new branch now holds. Either
    way the move is voided first, or the document is corrected at the new branch.
    """
    from .models import CreditNote, Invoice, Payment, ReceivableTransferItem

    lookup = {Invoice: "invoice", CreditNote: "note", Payment: "payment"}.get(type(document))
    if lookup is None:
        return None
    row = (
        ReceivableTransferItem.objects
        .filter(**{lookup: document}, transfer__status=DocumentStatus.POSTED)
        .select_related("transfer", "transfer__to_branch").first()
    )
    if row is None:
        return None
    where = row.transfer.to_branch.name
    return (
        f"{document.document_number} moved to {where} with the customer's balance "
        f"({row.transfer.document_number}). Void that move first, or correct it at {where}."
    )


def book_income_given_back(adjustment_entry, *, holder_branch_id, invoice, deferred=0, lines=None,
                           label="", actor_user=None):
    """Book the side of an adjustment that another branch's books bear.

    A receivable move carries a bill to a new branch while the revenue and
    output tax its invoice journal booked, and any month already earned, stay at
    the old branch. A credit note or concession raised at the new branch cancels
    that income, so the branch that booked it gives it back
    (:mod:`vs_finance.bill_adjustments`): the adjusting journal debits the
    inter-branch account naming that branch, and this posts that branch's
    journal: ``Dr deferred income`` for the waiting shares it holds, a debit on
    each account in ``lines`` (``{(account_id, cost_center_id): kobo}``: revenue
    or allowance, and output tax), and ``Cr inter-branch [adjusting branch]``
    for the total.

    Tunde's 100k textbook bill (plus 7.5k VAT) moved from Ikeja to Lekki, and
    Lekki owes Ikeja 107.5k for it. Lekki credits it in full: its credit note
    debits ``inter-branch [Ikeja] 107.5k`` against the receivable, and Ikeja
    books ``Dr revenue 100k, Dr output VAT 7.5k, Cr inter-branch [Lekki]
    107.5k``. Ikeja keeps no income for books never delivered, its share of the
    VAT return falls by 7.5k, Lekki owes it nothing, and the pair agrees.

    One ``INCOME_GIVEN_BACK`` transfer records it, from the adjusting branch to
    the branch giving the income back, linked to the adjusting journal. The
    adjusting branch's leg carries no journal, because its side is a line of the
    adjusting document's own journal. Voided only with that document
    (:func:`vs_finance.deferred_income.restore_unwinds`). Returns the transfer,
    or ``None`` when nothing is given back.
    """
    from .models import Account, InterBranchTransferLeg

    entity = adjustment_entry.entity
    lines = {key: int(value) for key, value in (lines or {}).items() if value}
    deferred = int(deferred or 0)
    amount = deferred + sum(lines.values())
    if amount <= 0:
        return None
    giver = tenant_branch(entity, adjustment_entry.branch_id)
    holder = tenant_branch(entity, holder_branch_id)
    label = (label or f"journal {adjustment_entry.document_number or adjustment_entry.pk}")[:64]
    transfer = _new_transfer(
        entity, kind=InterBranchTransferKind.INCOME_GIVEN_BACK, from_branch_id=giver.pk,
        to_branch_id=holder.pk, amount=amount, transfer_date=adjustment_entry.date,
        purpose=f"Income of invoice {invoice.document_number} booked at {holder.name}, "
                f"taken back by {label} at {giver.name}",
        actor_user=actor_user, customer_id=invoice.customer_id,
        adjustment_entry=adjustment_entry, reference=label,
    )
    InterBranchTransferLeg.objects.create(
        transfer=transfer, role=InterBranchLegRole.SENDING, branch_id=giver.pk,
        counterparty_branch_id=holder.pk, journal=None,
    )
    accounts = Account.objects.in_bulk([account_id for account_id, _ in lines])
    posting = []
    if deferred:
        posting.append((resolve_mapped_account(entity, AccountMappingKey.DEFERRED_INCOME), deferred, 0, None))
    for (account_id, cost_center_id), value in lines.items():
        posting.append((accounts[account_id], value, 0, None, cost_center_id))
    posting.append((inter_branch_account(entity), 0, amount, giver.pk))
    _post_leg(
        transfer, role=InterBranchLegRole.RECEIVING, branch_id=holder.pk, counterparty_id=giver.pk,
        source=JournalSource.SALES, actor_user=actor_user, lines=posting,
        narration=f"Income of invoice {invoice.document_number} given back for {label} at {giver.name}",
    )
    _finish(transfer)
    _audit_both(
        transfer, FinanceAuditAction.INCOME_GIVEN_BACK,
        f"{label} at {giver.name} took back {format_naira(amount)} of invoice "
        f"{invoice.document_number}'s income booked at {holder.name}.",
        actor_user=actor_user, invoice_id=invoice.pk, adjustment_journal_id=adjustment_entry.pk,
        deferred=deferred, other=sum(lines.values()),
    )
    return transfer


# --------------------------------------------------------------------------- #
# Recharges                                                                   #
# --------------------------------------------------------------------------- #

def split_by_weight(amount: int, weights: dict) -> dict:
    """Split ``amount`` kobo by ``weights`` (``{branch_id: weight}``), exactly.

    Largest remainder: each branch gets the whole kobo of its share, and the kobo
    left over go one each to the largest fractions, ties to the lower branch id.
    The shares always add up to ``amount``.
    """
    total = sum(weights.values())
    if total <= 0:
        raise InterBranchError("The weights must add up to more than nothing.", field="weights")
    base = {branch: amount * weight // total for branch, weight in weights.items()}
    left = amount - sum(base.values())
    order = sorted(weights, key=lambda b: (-(amount * weights[b] % total), b))
    for branch in order[:left]:
        base[branch] += 1
    return base


def recharge_weights(entity, *, basis, weights=None, rule=None) -> dict:
    """The ``{branch_id: weight}`` a recharge splits by.

    Counts are plain non-negative whole numbers for each branch, supplied when the
    recharge is run (the tenant's own figures, such as pupils per branch). Fixed
    percentages are basis points totalling 10000, supplied or, when none are
    supplied, read from ``rule``.
    """
    from vs_tenants.models import Branch

    if not weights and basis == RechargeBasis.PERCENTAGES and rule is not None:
        weights = {share.branch_id: share.percent_bps for share in rule.shares.all()}
    if not weights:
        raise InterBranchError("Give a weight for each branch sharing the cost.", field="weights")
    cleaned = {}
    for branch, weight in weights.items():
        branch_id = getattr(branch, "pk", branch)
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
            raise InterBranchError("Each weight must be a whole number, zero or more.", field="weights")
        cleaned[int(branch_id)] = weight
    known = set(
        Branch.all_objects.filter(tenant_id=entity.tenant_id, pk__in=cleaned).values_list("pk", flat=True)
    )
    if known != set(cleaned):
        raise InterBranchError("A weight names a branch outside these books.", field="weights")
    if basis == RechargeBasis.PERCENTAGES and sum(cleaned.values()) != WHOLE_BPS:
        raise InterBranchError("Fixed percentages must total 100.", field="weights")
    return cleaned


def run_recharge(entity, *, paying_branch, expense_account, amount, recharge_date, narration,
                 basis=None, weights=None, rule=None, reference="", actor_user=None):
    """Recharge a cost ``paying_branch`` paid to the branches that share it.

    Each other branch's share becomes a RECHARGE transfer from the paying branch:
    ``Dr inter-branch [owing], Cr expense`` at the paying branch and ``Dr expense,
    Cr inter-branch [paying]`` at the owing one. The paying branch's own share,
    if it has a weight, stays on its books. With a ``rule``, the rule decides:
    a cost it says the paying branch absorbs is refused, and its basis, fixed
    percentages and expense account are the defaults.
    """
    from .control_accounts import control_accounts
    from .models import InterBranchRecharge, InterBranchRechargeLine

    require_several_branches(entity)
    payer = tenant_branch(entity, paying_branch, field="branch")
    if rule is not None:
        if rule.entity_id != entity.pk:
            raise InterBranchError("No such shared cost rule in these books.", field="rule")
        if rule.treatment == SharedCostTreatment.ABSORB:
            raise InterBranchError(
                f"The tenant has chosen that the paying branch absorbs {rule.name}, so it is "
                f"not recharged. Change the rule to recharge it first.",
                field="rule",
            )
        basis = basis or rule.basis
        expense_account = expense_account or rule.expense_account
    basis = basis or RechargeBasis.COUNTS
    if basis not in RechargeBasis.values:
        raise InterBranchError("Choose counts or fixed percentages.", field="basis")
    if expense_account is None:
        raise InterBranchError("Name the expense account the cost was booked to.", field="expense_account")
    if (expense_account.entity_id != entity.pk or expense_account.account_type != AccountType.EXPENSE
            or not (expense_account.is_active and expense_account.is_postable)
            or expense_account.pk in control_accounts(entity)):
        raise InterBranchError(
            "The cost must be an active, postable expense account of these books.",
            field="expense_account",
        )
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InterBranchError("Recharge a positive whole amount in kobo.", field="amount")
    cleaned = recharge_weights(entity, basis=basis, weights=weights, rule=rule)
    shares = split_by_weight(amount, cleaned)
    owing = {branch: share for branch, share in shares.items() if branch != payer.pk and share > 0}
    if not owing:
        raise InterBranchError("No other branch takes a share of this cost.", field="weights")

    with transaction.atomic():
        ensure_branches_open(entity, recharge_date, payer, *owing)
        recharge = InterBranchRecharge.objects.create(
            entity=entity, branch=payer, rule=rule, expense_account=expense_account,
            amount=amount, recharge_date=recharge_date, basis=basis,
            narration=(narration or "")[:255], reference=(reference or "")[:64],
            created_by=actor_user,
        )
        for branch_id in sorted(cleaned):
            transfer = None
            if branch_id in owing:
                transfer = _new_transfer(
                    entity, kind=InterBranchTransferKind.RECHARGE, from_branch_id=payer.pk,
                    to_branch_id=branch_id, amount=owing[branch_id], transfer_date=recharge_date,
                    purpose=f"Share of {recharge.narration} ({recharge.document_number})",
                    actor_user=actor_user, recharge=recharge, reference=recharge.document_number,
                )
                _post_balance_pair(
                    transfer, sending_account=expense_account, receiving_account=expense_account,
                    source=JournalSource.SYSTEM, actor_user=actor_user,
                )
                _finish(transfer)
            InterBranchRechargeLine.objects.create(
                recharge=recharge, branch_id=branch_id, weight=cleaned[branch_id],
                amount=shares[branch_id], transfer=transfer,
            )
        recharge.status = DocumentStatus.POSTED
        recharge.save(update_fields=["status", "updated_at"])
        for branch_id in sorted({payer.pk, *owing}):
            record(
                entity=entity, action=FinanceAuditAction.RECHARGE_POSTED,
                actor_user=actor_user, target=recharge, branch=branch_id,
                message=(
                    f"{payer.name} recharged {format_naira(amount)} of {recharge.narration}"
                    + (f"; this branch's share is {format_naira(shares[branch_id])}."
                       if branch_id in owing else ".")
                ),
                amount=amount, share=int(shares.get(branch_id, 0)), basis=basis,
            )
    return recharge


@transaction.atomic
def void_recharge(recharge, *, actor_user=None, date=None):
    """Void a recharge and every share it booked, together."""
    from .models import InterBranchRecharge

    recharge = InterBranchRecharge.objects.select_for_update().get(pk=recharge.pk)
    if recharge.status != DocumentStatus.POSTED:
        raise InterBranchError("Only a posted recharge can be voided.")
    for transfer in recharge.transfers.filter(status=DocumentStatus.POSTED).order_by("pk"):
        _void_transfer_atomic(transfer, actor_user=actor_user, date=date, recharge=recharge)
    recharge.status = DocumentStatus.REVERSED
    recharge.save(update_fields=["status", "updated_at"])
    branches = {recharge.branch_id, *recharge.lines.exclude(transfer=None).values_list("branch_id", flat=True)}
    for branch_id in sorted(branches):
        record(
            entity=recharge.entity, action=FinanceAuditAction.RECHARGE_VOIDED,
            actor_user=actor_user, target=recharge, branch=branch_id,
            message=f"Voided recharge {recharge.document_number}.", amount=int(recharge.amount),
        )
    return recharge


# --------------------------------------------------------------------------- #
# Goods                                                                       #
# --------------------------------------------------------------------------- #

def book_goods_transfer(entity, *, from_branch, to_branch, amount, transfer_date, inventory_account,
                        purpose, reference="", actor_user=None):
    """Book goods one branch's store issued to another's, at their moving-average cost.

    The sending branch's inventory goes down and it is owed the cost; the
    receiving branch's goes up and it owes the cost. The stock ledger moves the
    goods themselves (:func:`vs_procurement.stock.transfer_stock`), which calls
    this inside its own transaction and links each movement to its branch's
    journal. Returns the posted transfer.
    """
    require_several_branches(entity)
    source = tenant_branch(entity, from_branch, field="from_location")
    target = tenant_branch(entity, to_branch, field="to_location")
    with transaction.atomic():
        ensure_branches_open(entity, transfer_date, source, target)
        transfer = _new_transfer(
            entity, kind=InterBranchTransferKind.GOODS, from_branch_id=source.pk,
            to_branch_id=target.pk, amount=amount, transfer_date=transfer_date,
            purpose=purpose, actor_user=actor_user, reference=(reference or "")[:64],
        )
        _post_balance_pair(
            transfer, sending_account=inventory_account, receiving_account=inventory_account,
            source=JournalSource.PURCHASE, actor_user=actor_user,
        )
        _finish(transfer)
        _audit_both(
            transfer, FinanceAuditAction.INTER_BRANCH_SENT,
            f"{source.name} issued goods worth {format_naira(amount)} to {target.name}: {purpose}",
            actor_user=actor_user,
        )
    return transfer


def book_bank_split_difference(entity, *, from_branch, to_branch, amount, split_date,
                               shared_ledger, purpose, reference="", actor_user=None):
    """Book cash one branch gives up to another when a shared bank account is split.

    ``from_branch``'s entries on the shared ledger came to more than the share it
    agreed to take, and ``to_branch``'s to less, so ``to_branch`` now holds
    ``amount`` of ``from_branch``'s cash and owes it: ``Dr inter-branch
    [to_branch], Cr shared ledger`` at ``from_branch`` and ``Dr shared ledger, Cr
    inter-branch [from_branch]`` at ``to_branch``. The shared ledger's total is
    unchanged; each branch's side of it moves to its agreed share, ready to be
    carried to that branch's own bank ledger. Called inside the split's
    transaction (:func:`vs_finance.bank_splits.split_shared_bank_account`), which
    has already checked every branch is open on ``split_date``. Returns the posted
    transfer.
    """
    require_several_branches(entity)
    source = tenant_branch(entity, from_branch, field="branch")
    target = tenant_branch(entity, to_branch, field="branch")
    with transaction.atomic():
        ensure_branches_open(entity, split_date, source, target)
        transfer = _new_transfer(
            entity, kind=InterBranchTransferKind.BANK_SPLIT, from_branch_id=source.pk,
            to_branch_id=target.pk, amount=amount, transfer_date=split_date,
            purpose=purpose, actor_user=actor_user, reference=(reference or "")[:64],
        )
        _post_balance_pair(
            transfer, sending_account=shared_ledger, receiving_account=shared_ledger,
            source=JournalSource.SYSTEM, actor_user=actor_user,
        )
        _finish(transfer)
        _audit_both(
            transfer, FinanceAuditAction.INTER_BRANCH_SENT,
            f"{target.name} owes {source.name} {format_naira(amount)} agreed when a shared "
            f"bank account was split: {purpose}",
            actor_user=actor_user,
        )
    return transfer


def leg_journals(transfer) -> dict:
    """``{role: journal}`` of a posted transfer's two sides."""
    return {leg.role: leg.journal for leg in transfer.legs.select_related("journal")}


# --------------------------------------------------------------------------- #
# Reading                                                                     #
# --------------------------------------------------------------------------- #

def transfers_visible_q(scope):
    """The transfers a reader with ``scope`` sees: sending branch OR receiving branch in reach.

    ``scope`` is the reader's :func:`vs_rbac.scoping.transaction_branch_scope`. A
    whole-tenant reader is not narrowed at all.
    """
    if not scope.is_narrowed:
        return Q()
    return scope.q() | scope.q(field="to_branch")


def pair_balances(entity, *, branch_ids=None) -> dict:
    """Who owes whom between ``entity``'s branches, from the inter-branch ledger.

    Read from every journal line on the inter-branch balances account, summed per
    (branch whose books hold the line, counterparty). On Ikeja's books a net debit
    against Lekki is what Lekki owes Ikeja; Lekki's books hold the same figure as
    a net credit against Ikeja, so each pair is reported once, as ``owed_by``
    owes ``owed_to`` ``amount``, and a pair whose two sides disagree is flagged
    ``balanced: False``. Money held for another branch is reported beside it.

    ``branch_ids`` narrows to pairs with at least one of those branches, which is
    what a branch-bound reader sees: Ikeja's bursar sees what Lekki and Yaba owe
    Ikeja, never what Yaba owes Lekki. ``None`` is the whole tenant.
    """
    from vs_tenants.models import Branch

    from .branch_ledger import ledger_lines

    def sums(account):
        rows = (
            ledger_lines(entity).filter(account=account, counterparty_branch__isnull=False)
            .values("entry__branch_id", "counterparty_branch_id")
            .annotate(dr=Sum("debit"), cr=Sum("credit"))
        )
        return {(r["entry__branch_id"], r["counterparty_branch_id"]): int(r["dr"] or 0) - int(r["cr"] or 0)
                for r in rows}

    try:
        net = sums(inter_branch_account(entity))
        held = sums(held_for_account(entity))
    except FinanceError:
        net, held = {}, {}
    names = dict(Branch.all_objects.filter(tenant_id=entity.tenant_id).values_list("pk", "name"))

    def visible(a, b):
        return branch_ids is None or a in branch_ids or b in branch_ids

    pairs = []
    for a, b in sorted({tuple(sorted(key)) for key in net if None not in key}):
        if not visible(a, b):
            continue
        a_books, b_books = net.get((a, b), 0), net.get((b, a), 0)
        if a_books == 0 and b_books == 0:
            continue
        figure = a_books if a_books else -b_books
        owed_to, owed_by = (a, b) if figure > 0 else (b, a)
        pairs.append({
            "owed_to": {"id": owed_to, "name": names.get(owed_to, "")},
            "owed_by": {"id": owed_by, "name": names.get(owed_by, "")},
            "amount": abs(figure),
            "balanced": a_books == -b_books,
        })
    holding = [
        {"held_by": {"id": a, "name": names.get(a, "")},
         "held_for": {"id": b, "name": names.get(b, "")}, "amount": -figure}
        for (a, b), figure in sorted(held.items())
        if a is not None and figure != 0 and visible(a, b)
    ]
    total = None if branch_ids is not None else sum(net.values())
    return {"pairs": pairs, "held": holding, "net_total": total}


# --------------------------------------------------------------------------- #
# Period close                                                                #
# --------------------------------------------------------------------------- #

def inter_branch_close_check(entity, period, *, branch=None):
    """Close check: the inter-branch account nets to zero, and each pair's two sides agree.

    Read from every ledger line on the inter-branch account dated by the end of
    ``period``. Each transfer books the same amount on both sides, so what Lekki
    owes Ikeja on Ikeja's books equals what Lekki owes Ikeja on Lekki's books, and
    the account across every branch is zero. A line naming no counterparty, or
    sitting in no branch's books, cannot be paired and fails the check too.

    The tenant's close (no ``branch``) is blocked by a failure: sealing a month
    whose inter-branch balances do not pair carries the difference into every
    later statement. A single branch's close passes ``branch`` and reads only the
    pairs that branch is part of, as a warning: the fault may sit in the other
    branch's books, which that branch cannot correct.
    """
    from .branch_ledger import ledger_lines
    from .close import ChecklistItem

    name = inter_branch_close_check.check_name
    branch_id = getattr(branch, "pk", branch)
    blocking = branch_id is None
    try:
        account = inter_branch_account(entity)
    except FinanceError:
        return ChecklistItem(name=name, passed=True, blocking=blocking,
                             detail="no inter-branch balances account")
    rows = (
        ledger_lines(entity).filter(account=account, entry__date__lte=period.end_date)
        .values("entry__branch_id", "counterparty_branch_id")
        .annotate(dr=Sum("debit"), cr=Sum("credit"))
    )
    net = {
        (r["entry__branch_id"], r["counterparty_branch_id"]): int(r["dr"] or 0) - int(r["cr"] or 0)
        for r in rows
    }

    def concerns(*ids):
        return branch_id is None or branch_id in ids

    problems = []
    unpaired = sum(
        1 for (own, other), amount in net.items()
        if amount and (own is None or other is None) and concerns(own, other)
    )
    if unpaired:
        problems.append(f"{unpaired} inter-branch balance(s) name no branch or no counterparty")
    for a, b in sorted({tuple(sorted(key)) for key in net if None not in key}):
        if concerns(a, b) and net.get((a, b), 0) != -net.get((b, a), 0):
            problems.append(
                f"branches {a} and {b} disagree: {net.get((a, b), 0)} kobo on one side and "
                f"{-net.get((b, a), 0)} kobo on the other",
            )
    if blocking and sum(net.values()):
        problems.append(f"the inter-branch account nets to {sum(net.values())} kobo, not zero")
    return ChecklistItem(
        name=name, passed=not problems, blocking=blocking,
        detail="; ".join(problems) or "inter-branch balances net to zero and every pair agrees",
    )


inter_branch_close_check.check_name = "inter_branch_balanced"
inter_branch_close_check.supports_branch = True
