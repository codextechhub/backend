"""One payer's payment, split into one receipt per customer it pays for.

Mr Okafor has three children at Bright Star: Ada and Emeka billed at Ikeja,
Chidi at Lekki. He sends N450,000 in one transfer into Ikeja's bank. The books
never let one receipt settle another customer's bill, nor one branch's receipt
settle another branch's bill (:func:`vs_finance.receivables._require_settlable_targets`),
so the payment is recorded once, as a :class:`~vs_finance.models.PayerPayment`,
and split:

* each customer's share of bills at the bank's own branch is an ordinary receipt
  of that branch (Ada's and Emeka's), applied to exactly the bills the split
  chose;
* each share of bills at another branch (Chidi's, at Lekki) is money Ikeja holds
  for Lekki (:class:`~vs_finance.models.HeldForBranchReceipt`, ``Dr Ikeja's bank,
  Cr held for other branches [Lekki]``). Ikeja forwards it with the usual
  forwarded-receipt transfer (:func:`vs_finance.inter_branch.forward_held_receipt`),
  and at Lekki it becomes Chidi's receipt and settles his Lekki bills oldest
  first. Lekki's bill is paid when the money reaches Lekki, as the branch design
  requires.

Who may be paid for is fixed by :class:`~vs_finance.models.PayerLink`: the payer
itself and the customers linked to it, nobody else.

How the money is shared when it is not enough for everything is the tenant's
choice (``payer_payment_split`` on the receivables policy): every open bill of
every customer, oldest first, as one list (the default); in proportion to what
each customer owes; or as the bursar enters it. The bursar may always override
the proposal with an amount per customer. When there is more than the bills
take, the rest becomes credit on the customer with the most recent bill (the
default ``payer_payment_surplus``) or on the payer's own account, at the branch of
that customer's newest bill; the books' rule for applying credit to new bills
(``auto_apply_customer_credit``) then takes over.

The split is proposed by :func:`plan_payer_payment`, which writes nothing, and
booked by :func:`record_payer_payment`, which books every share in one
transaction. Voiding (:func:`void_payer_payment`) voids every receipt and held
receipt it made, together; none of them can be voided on its own.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from .audit import record, record_rejection
from .constants import (
    CreditNoteKind,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    PayerPaymentSplit,
    PayerPaymentSurplus,
    PaymentMethod,
)
from .exceptions import FinanceError, PayerPaymentError
from .money import format_naira
from .wording import state_word

#: The split recorded when the bursar typed each customer's amount.
EXPLICIT_SPLIT = "EXPLICIT"


# --------------------------------------------------------------------------- #
# Who a payer pays for                                                        #
# --------------------------------------------------------------------------- #

def payer_customers(payer) -> list:
    """The payer itself and every customer it is actively linked to, payer first.

    The payer is included because a payer may be billed in its own right (a
    sponsor billed for the people it sponsors), and its own bills are paid by its
    own money like anybody else's.
    """
    from .models import Customer

    linked = (
        Customer.objects.filter(
            entity_id=payer.entity_id, paid_by_links__payer=payer, paid_by_links__is_active=True,
        )
        .exclude(pk=payer.pk).distinct().order_by("pk")
    )
    return [payer, *linked]


def link_customer(payer, customer, *, actor_user=None, source_type="", source_id=""):
    """Record that ``payer`` pays for ``customer``, or switch an ended link back on.

    Idempotent: linking a pair already linked changes nothing and writes no audit.
    Both must be customers of the same books, and a customer cannot pay for itself.
    """
    from .models import PayerLink

    if payer.pk == customer.pk:
        raise PayerPaymentError("A customer cannot be its own payer.", field="customer")
    if payer.entity_id != customer.entity_id:
        raise PayerPaymentError("The payer and the customer belong to different books.", field="customer")
    link, created = PayerLink.objects.get_or_create(
        payer=payer, customer=customer,
        defaults={
            "entity_id": payer.entity_id, "created_by": actor_user,
            "source_type": source_type[:64], "source_id": str(source_id)[:64],
        },
    )
    if not created and link.is_active:
        return link
    if not created:
        link.is_active = True
        link.save(update_fields=["is_active", "updated_at"])
    record(
        entity=payer.entity, action=FinanceAuditAction.PAYER_LINK_CHANGED, actor_user=actor_user,
        target=link, branch=None,
        message=f"{payer.name} ({payer.code}) pays for {customer.name} ({customer.code}).",
        payer=payer.code, customer=customer.code, active=True,
    )
    return link


def unlink_customer(link, *, actor_user=None):
    """End a link. Payments already made under it keep their shares."""
    if not link.is_active:
        return link
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    record(
        entity=link.entity, action=FinanceAuditAction.PAYER_LINK_CHANGED, actor_user=actor_user,
        target=link, branch=None,
        message=f"{link.payer.name} ({link.payer.code}) no longer pays for "
                f"{link.customer.name} ({link.customer.code}).",
        payer=link.payer.code, customer=link.customer.code, active=False,
    )
    return link


# --------------------------------------------------------------------------- #
# The plan                                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class PlannedShare:
    """What one customer receives at one branch: the bills it settles and any credit."""

    customer: object
    branch_id: int
    items: list = field(default_factory=list)
    surplus: int = 0

    @property
    def settled(self) -> int:
        return sum(amount for _target, amount in self.items)

    @property
    def amount(self) -> int:
        return self.settled + self.surplus


@dataclass
class PayerPlan:
    """A proposed split of one payment. ``shares`` are keyed by (customer id, branch id)."""

    payer: object
    bank_account: object
    branch_id: int
    amount: int
    payment_date: object
    split: str
    shares: dict
    outstanding: dict

    def ordered_shares(self) -> list:
        """The shares, the bank's own branch first, then by branch and customer."""
        return sorted(
            (s for s in self.shares.values() if s.amount > 0),
            key=lambda s: (s.branch_id != self.branch_id, s.branch_id, s.customer.pk),
        )


@dataclass(frozen=True)
class _OpenItem:
    target: object
    customer_id: int
    branch_id: int
    balance: int
    sort_date: object


def _settling_branch(branch_id, only_branch):
    """The branch a bill is settled at: its own, or the only branch for a legacy blank one."""
    if branch_id is not None:
        return branch_id
    return only_branch


def open_items(entity, customer_ids, *, as_of) -> list:
    """Every open bill (invoices and debit notes) of ``customer_ids``, oldest first.

    The same bills, in the same order, that a receipt's automatic settlement
    takes (:func:`vs_finance.receivables._build_invoice_plan`): posted, not paid,
    raised on or before ``as_of`` (a payment cannot clear a bill raised after it),
    by due date (or document date) and then by id. A bill not yet given a branch
    is the only branch's at a tenant with one; at a tenant with several no receipt
    may settle it, so it is left out.
    """
    from vs_rbac.scoping import only_branch_id

    from .models import CreditNote, Invoice

    only = only_branch_id(entity.tenant)
    rows = []
    invoices = (
        Invoice.objects.filter(
            entity=entity, customer_id__in=customer_ids, status=DocumentStatus.POSTED,
            invoice_date__lte=as_of,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
    )
    for inv in invoices:
        branch_id = _settling_branch(inv.branch_id, only)
        if branch_id is not None and inv.balance_due > 0:
            rows.append(_OpenItem(inv, inv.customer_id, branch_id, inv.balance_due,
                                  inv.due_date or inv.invoice_date))
    notes = (
        CreditNote.objects.filter(
            entity=entity, customer_id__in=customer_ids, status=DocumentStatus.POSTED,
            kind=CreditNoteKind.DEBIT, note_date__lte=as_of,
        ).exclude(settlement_status=InvoicePaymentStatus.PAID)
    )
    for note in notes:
        branch_id = _settling_branch(note.branch_id, only)
        if branch_id is not None and note.balance_due > 0:
            rows.append(_OpenItem(note, note.customer_id, branch_id, note.balance_due, note.note_date))
    rows.sort(key=lambda item: (item.sort_date, item.customer_id, item.target.pk))
    return rows


def _newest_bill(entity, customer_ids):
    """The (customer id, branch id) of the newest posted invoice among ``customer_ids``, or None."""
    from vs_rbac.scoping import only_branch_id

    from .models import Invoice

    only = only_branch_id(entity.tenant)
    qs = Invoice.objects.filter(
        entity=entity, customer_id__in=customer_ids, status=DocumentStatus.POSTED,
    )
    if only is None:
        qs = qs.filter(branch__isnull=False)
    newest = qs.order_by("-invoice_date", "-pk").values("customer_id", "branch_id").first()
    if newest is None:
        return None
    return newest["customer_id"], newest["branch_id"] or only


def _credit_branch(entity, customer, doc_branch_id) -> int:
    """Where credit left with ``customer`` is kept: the branch of their newest bill.

    That is the branch most likely to bill them next, so it is where the credit is
    applied to their next bill. A customer never billed keeps it at the branch
    they are filed under, and a customer every branch shares keeps it at the
    branch that received the money.
    """
    newest = _newest_bill(entity, [customer.pk])
    if newest is not None:
        return newest[1]
    return customer.branch_id or doc_branch_id


def _surplus_holder(entity, payer, customers, doc_branch_id, rule):
    """The customer and branch the part of the payment no bill takes is left with."""
    if rule == PayerPaymentSurplus.PAYER:
        return payer, payer.branch_id or doc_branch_id
    newest = _newest_bill(entity, [c.pk for c in customers])
    if newest is None:
        return payer, payer.branch_id or doc_branch_id
    by_id = {c.pk: c for c in customers}
    return by_id[newest[0]], newest[1]


def _walk(items, amount, take):
    """Give ``amount`` to ``items`` in order, each up to its balance; returns what is left."""
    for item in items:
        if amount <= 0:
            break
        portion = min(item.balance, amount)
        take(item, portion)
        amount -= portion
    return amount


def plan_payer_payment(payer, *, bank_account, amount, payment_date, shares=None, split=None):
    """Propose how ``amount`` from ``payer`` is shared, writing nothing.

    ``shares`` is the bursar's own split, ``[(customer, amount)]``: each customer's
    amount goes to that customer's open bills oldest first, and whatever their
    bills do not take stays as their credit. Without it the split is ``split``, or
    the tenant's ``payer_payment_split``; ``AS_ENTERED`` proposes nothing and asks
    for the amounts. What the split leaves over goes to the surplus holder.

    Every bill is read whatever branch it sits at, because a payer's money may be
    meant for any of them; the shares say which branch each part belongs to, and
    :func:`record_payer_payment` books each part at its own branch.
    """
    from vs_rbac.scoping import only_branch_id

    from .inter_branch import split_by_weight
    from .receivables_policy import resolve_receivables_policy

    entity = payer.entity
    amount = int(amount or 0)
    if amount <= 0:
        raise PayerPaymentError("A payment must be for a positive amount.", field="amount")
    if bank_account.entity_id != entity.pk:
        raise PayerPaymentError("The bank account belongs to other books.", field="bank_account")
    if not bank_account.is_active:
        raise PayerPaymentError(f"Bank account {bank_account.name} is closed.", field="bank_account")
    doc_branch_id = bank_account.branch_id or only_branch_id(entity.tenant)
    if doc_branch_id is None:
        raise PayerPaymentError(
            f"{bank_account.name} has not been given a branch, so no branch can receive money "
            f"into it. Give it its branch first.",
            field="bank_account",
        )

    policy = resolve_receivables_policy(entity)
    customers = payer_customers(payer)
    by_id = {c.pk: c for c in customers}
    items = open_items(entity, list(by_id), as_of=payment_date)
    outstanding = defaultdict(int)
    for item in items:
        outstanding[(item.customer_id, item.branch_id)] += item.balance

    planned: dict = {}

    def share_for(customer, branch_id):
        return planned.setdefault((customer.pk, branch_id), PlannedShare(customer, branch_id))

    def take(item, portion):
        share_for(by_id[item.customer_id], item.branch_id).items.append((item.target, portion))

    if shares:
        mode = EXPLICIT_SPLIT
        seen = set()
        for customer, given in shares:
            given = int(given or 0)
            if customer.pk not in by_id:
                raise PayerPaymentError(
                    f"{payer.name} does not pay for {customer.name}. Link them first, or "
                    f"record {customer.name}'s payment on its own.",
                    field="shares",
                )
            if customer.pk in seen:
                raise PayerPaymentError(f"{customer.name} is named twice.", field="shares")
            if given <= 0:
                raise PayerPaymentError(f"Give {customer.name} a positive amount.", field="shares")
            seen.add(customer.pk)
        entered = sum(int(given) for _c, given in shares)
        if entered > amount:
            raise PayerPaymentError(
                f"The amounts entered come to {format_naira(entered)}, more than the "
                f"{format_naira(amount)} received.",
                field="shares",
            )
        for customer, given in shares:
            own = [item for item in items if item.customer_id == customer.pk]
            left = _walk(own, int(given), take)
            if left > 0:
                share_for(customer, _credit_branch(entity, customer, doc_branch_id)).surplus += left
        remaining = amount - entered
    else:
        mode = split or policy.payer_payment_split
        if mode not in PayerPaymentSplit.values:
            raise PayerPaymentError("Unknown way of splitting the payment.", field="split")
        if mode == PayerPaymentSplit.AS_ENTERED:
            raise PayerPaymentError(
                "These books split a payer's payment as the bursar enters it. Give each "
                "customer's amount.",
                field="shares",
            )
        if mode == PayerPaymentSplit.PROPORTIONAL:
            owed = defaultdict(int)
            for item in items:
                owed[item.customer_id] += item.balance
            total = sum(owed.values())
            portions = owed if amount >= total else split_by_weight(amount, dict(owed))
            for customer_id, portion in portions.items():
                own = [item for item in items if item.customer_id == customer_id]
                _walk(own, int(portion), take)
            remaining = amount - min(amount, total)
        else:
            remaining = _walk(items, amount, take)

    if remaining > 0:
        holder, branch_id = _surplus_holder(
            entity, payer, customers, doc_branch_id, policy.payer_payment_surplus,
        )
        share_for(holder, branch_id).surplus += remaining

    return PayerPlan(
        payer=payer, bank_account=bank_account, branch_id=doc_branch_id, amount=amount,
        payment_date=payment_date, split=mode, shares=planned, outstanding=dict(outstanding),
    )


# --------------------------------------------------------------------------- #
# Booking                                                                     #
# --------------------------------------------------------------------------- #

def record_payer_payment(payer, *, bank_account, amount, payment_date, method=PaymentMethod.BANK_TRANSFER,
                         reference="", narration="", shares=None, split=None, actor_user=None):
    """Book one payment from ``payer``, recording a durable rejection when refused."""
    try:
        return _record_atomic(
            payer, bank_account=bank_account, amount=amount, payment_date=payment_date,
            method=method, reference=reference, narration=narration, shares=shares,
            split=split, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=payer.entity, action=FinanceAuditAction.PAYER_PAYMENT_POSTED,
            exc=exc, actor_user=actor_user, target=payer,
        )
        raise


@transaction.atomic
def _record_atomic(payer, *, bank_account, amount, payment_date, method, reference, narration,
                   shares, split, actor_user):
    """Plan the split and book every share in one transaction.

    The payer's account is locked first, so two payments from the same payer
    queue rather than both planning against the same open bills. The period is
    checked before anything is written, so a closed month refuses the whole
    payment rather than part of it. Each share at the bank's own branch is a
    receipt applied to exactly the bills the plan chose; settlement re-reads each
    bill under its row lock, so a bill paid meanwhile takes only what it still
    owes and the rest stays as that customer's credit. Each share at another
    branch is a held receipt for that branch.
    """
    from .inter_branch import post_held_receipt, validate_held_receipt
    from .models import Customer, HeldForBranchReceipt, Payment, PayerPayment, PayerPaymentShare
    from .posting import ensure_date_open
    from .receivables import post_payment

    Customer.objects.select_for_update().get(pk=payer.pk)
    if method not in PaymentMethod.values:
        raise PayerPaymentError("Unknown payment method.", field="method")
    ensure_date_open(payer.entity, payment_date, branch=bank_account.branch_id)  # The bank's branch books it.
    plan = plan_payer_payment(
        payer, bank_account=bank_account, amount=amount, payment_date=payment_date,
        shares=shares, split=split,
    )
    document = PayerPayment.objects.create(
        entity=payer.entity, branch_id=plan.branch_id, payer=payer, bank_account=bank_account,
        amount=plan.amount, payment_date=payment_date, method=method, split=plan.split,
        reference=(reference or "")[:64], narration=(narration or "")[:255], created_by=actor_user,
    )
    said = (narration or f"Paid by {payer.name} ({document.document_number})")[:255]
    for share in plan.ordered_shares():
        customer = share.customer
        if share.branch_id == plan.branch_id:
            receipt = Payment.objects.create(
                entity=payer.entity, branch_id=plan.branch_id, customer=customer,
                payment_date=payment_date, method=method, amount=share.amount,
                deposit_account=bank_account.gl_account,
                reference=(reference or document.document_number)[:64], narration=said,
                created_by=actor_user,
            )
            post_payment(receipt, actor_user=actor_user, auto_allocate=False, allocations=share.items)
            PayerPaymentShare.objects.create(
                payer_payment=document, customer=customer, branch_id=share.branch_id,
                amount=share.amount, surplus=receipt.amount - receipt.allocated_amount,
                receipt=receipt,
            )
            continue
        held = HeldForBranchReceipt(
            entity=payer.entity, branch_id=plan.branch_id, bank_account=bank_account,
            for_branch_id=share.branch_id, customer=customer, amount=share.amount,
            receipt_date=payment_date, method=method,
            reference=(reference or document.document_number)[:64], narration=said,
            created_by=actor_user,
        )
        validate_held_receipt(held)
        held.save()
        post_held_receipt(held, actor_user=actor_user)
        PayerPaymentShare.objects.create(
            payer_payment=document, customer=customer, branch_id=share.branch_id,
            amount=share.amount, surplus=share.surplus, held_receipt=held,
        )
    document.status = DocumentStatus.POSTED
    document.save(update_fields=["status", "updated_at"])
    _audit_by_branch(document, FinanceAuditAction.PAYER_PAYMENT_POSTED, actor_user=actor_user)
    return document


def _audit_by_branch(document, action, *, actor_user=None, **metadata):
    """Write one entry per branch the payment's shares belong to, with that branch's figures.

    The branch that received the money also learns what it holds for others,
    because the money sits in its bank; another branch reads only its own
    customers' shares, never the receiving branch's. The entries' amounts add up
    to the whole payment.
    """
    shares = list(document.shares.select_related("customer", "branch", "receipt", "held_receipt"))
    by_branch = defaultdict(list)
    for share in shares:
        by_branch[share.branch_id].append(share)
    by_branch.setdefault(document.branch_id, [])
    voided = action == FinanceAuditAction.PAYER_PAYMENT_VOIDED
    verb = "Voided" if voided else "Recorded"
    for branch_id, own in by_branch.items():
        figure = sum(int(s.amount) for s in own)
        extra = dict(metadata)
        if branch_id == document.branch_id:
            held = int(document.amount) - figure
            extra.update(received=int(document.amount), held_for_other_branches=held)
            message = (
                f"{verb} {format_naira(document.amount)} from {document.payer.name} into "
                f"{document.bank_account.name}: {format_naira(figure)} for "
                f"{len(own)} customer(s) here"
                + (f", {format_naira(held)} held for other branches." if held else ".")
            )
        else:
            message = (
                f"{verb} {format_naira(figure)} from {document.payer.name}, received at "
                f"{document.branch.name} for {len(own)} customer(s) here."
            )
        record(
            entity=document.entity, action=action, actor_user=actor_user, target=document,
            branch=branch_id, message=message, amount=figure, payer=document.payer.code,
            shares=[{
                "customer": s.customer.code, "amount": int(s.amount), "credit": int(s.surplus),
                "document": (s.receipt or s.held_receipt).document_number,
            } for s in own],
            **extra,
        )


# --------------------------------------------------------------------------- #
# Voiding                                                                     #
# --------------------------------------------------------------------------- #

def void_payer_payment(document, *, actor_user=None, date=None):
    """Void a payer payment and everything it booked, recording a durable rejection when refused."""
    try:
        return _void_atomic(document, actor_user=actor_user, date=date)
    except FinanceError as exc:
        record_rejection(
            entity=document.entity, action=FinanceAuditAction.PAYER_PAYMENT_VOIDED,
            exc=exc, actor_user=actor_user, target=document,
        )
        raise


@transaction.atomic
def _void_atomic(document, *, actor_user=None, date=None):
    """Void every receipt and held receipt the payment made, together, or none of them.

    Refused while a held share is forwarded (or on its way) to its branch: that
    money has reached, or is about to reach, the other branch's bills, and voiding
    the forward first reverses both branches' sides. Refused too once a bank
    statement line is matched to any of its journals, and wherever voiding one of
    its receipts alone would be refused (a refund or credit transfer drew on its
    credit, or a receivable move carried that credit to another branch).
    """
    from .banking import journal_is_reconciled
    from .inter_branch import void_held_receipt
    from .models import InterBranchTransfer, PayerPayment
    from .voids import _void_payment_atomic

    document = PayerPayment.objects.select_for_update().get(pk=document.pk)
    if document.status != DocumentStatus.POSTED:
        raise PayerPaymentError(
            f"Only a posted payment can be voided; {document.document_number} is {state_word(document)}.",
        )
    shares = list(document.shares.select_related("receipt", "held_receipt", "customer").order_by("pk"))
    held_ids = [s.held_receipt_id for s in shares if s.held_receipt_id]
    forward = InterBranchTransfer.objects.filter(
        held_receipt_id__in=held_ids,
        status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED, DocumentStatus.POSTED),
    ).select_related("held_receipt").first()
    if forward is not None:
        raise PayerPaymentError(
            f"{forward.held_receipt.document_number} of this payment is forwarded by "
            f"{forward.document_number}. Void that transfer first.",
        )
    journals = [s.receipt.journal_id for s in shares if s.receipt_id]
    journals += [s.held_receipt.journal_id for s in shares if s.held_receipt_id]
    if any(journal_is_reconciled(j) for j in journals if j):
        raise PayerPaymentError(
            f"Payment {document.document_number} is matched to a bank statement line. "
            f"Unmatch it on the reconciliation first.",
        )
    for share in shares:
        if share.receipt_id:
            _void_payment_atomic(share.receipt, actor_user=actor_user, date=date, payer_payment=document)
        else:
            void_held_receipt(share.held_receipt, actor_user=actor_user, date=date, payer_payment=document)
    document.status = DocumentStatus.REVERSED
    document.save(update_fields=["status", "updated_at"])
    _audit_by_branch(document, FinanceAuditAction.PAYER_PAYMENT_VOIDED, actor_user=actor_user)
    return document


def payer_payment_refusal(document_kind, document, given=None):
    """The refusal for voiding one of a payer payment's own documents on its own, or None.

    ``document_kind`` is ``"receipt"`` or ``"held_receipt"``; ``given`` is the payer
    payment voiding it, which is the only caller allowed to.
    """
    from .models import PayerPaymentShare

    lookup = {document_kind: document}
    share = PayerPaymentShare.objects.filter(**lookup).select_related("payer_payment").first()
    if share is None or (given is not None and given.pk == share.payer_payment_id):
        return None
    number = share.payer_payment.document_number
    return (
        f"{document.document_number} is part of payment {number}, received from one payer "
        f"for several customers. Void {number} instead, which voids all of its receipts together."
    )
