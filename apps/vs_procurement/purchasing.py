"""Purchasing services — the procure side of Procure-to-Pay.

Covers the steps *before* a bill exists: approving a requisition, turning it into a
purchase order, and receiving goods. Only the goods receipt touches the General
Ledger — and it does so through :func:`vs_finance.posting.post_journal`, so the same
period-lock and balance guards that protect every other posting apply here too.

The receipt journal is the first half of the GR/IR control:

    **Dr expense/inventory, Cr GR/IR clearing** (accepted value, ex-tax)

recognising the cost on arrival while parking the liability in GR/IR until the
vendor's invoice clears it (see :mod:`vs_procurement.payables`).
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import F

from vs_finance.audit import record, record_rejection
from vs_finance.constants import DocumentStatus, FinanceAuditAction, JournalSource
from vs_finance.exceptions import FinanceError, PostingError
from vs_finance.posting import post_journal, resolve_period
from vs_finance.receivables import compute_line_net, compute_tax

from .constants import GRIR_CLEARING_CODE
from .exceptions import MissingControlAccountError, RequisitionError


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def resolve_account(entity, code: str, *, label: str = ""):
    """Return the active, postable :class:`Account` with ``code`` for ``entity``.

    Used to find well-known control accounts (GR/IR clearing, WHT payable) by their
    Chart-of-Accounts code. Raises :class:`MissingControlAccountError` when absent so a
    misconfigured entity fails loudly rather than posting into the wrong account.
    """
    from vs_finance.models import Account

    account = (
        Account.objects
        .filter(entity=entity, code=code, is_active=True, is_postable=True)
        .first()
    )
    if account is None:
        raise MissingControlAccountError(code, label=label)
    return account


def price_po(po) -> None:
    """Compute each PO line's ``net_amount``/``tax_amount`` and roll up the totals."""
    from .models import PurchaseOrderLine

    for line in po.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            PurchaseOrderLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    po.recompute_totals(save=True)


# --------------------------------------------------------------------------- #
# Requisition lifecycle (no GL effect)                                         #
# --------------------------------------------------------------------------- #

def submit_requisition(requisition, *, actor_user=None):
    """Move a draft requisition into PENDING_APPROVAL (the ``vs_workflow`` hand-off)."""
    if requisition.status != DocumentStatus.DRAFT:
        raise RequisitionError(
            f"Requisition {requisition.document_number or requisition.pk} is "
            f"'{requisition.status}', only a draft can be submitted.",
        )
    requisition.recompute_total(save=False)
    requisition.status = DocumentStatus.PENDING_APPROVAL
    requisition.save(update_fields=["status", "estimated_total", "updated_at"])
    return requisition


def approve_requisition(requisition, *, actor_user=None):
    """Approve a submitted requisition so it can become a purchase order.

    Approval routing by amount belongs to ``vs_workflow``; this is the state change the
    workflow drives to. Recorded in the finance audit log.
    """
    if requisition.status not in (DocumentStatus.PENDING_APPROVAL, DocumentStatus.DRAFT):
        raise RequisitionError(
            f"Requisition {requisition.document_number or requisition.pk} is "
            f"'{requisition.status}' and cannot be approved.",
        )
    requisition.status = DocumentStatus.APPROVED
    requisition.save(update_fields=["status", "updated_at"])
    record(
        entity=requisition.entity, action=FinanceAuditAction.REQUISITION_APPROVED,
        actor_user=actor_user, target=requisition,
        message=f"Approved requisition {requisition.document_number or requisition.pk}.",
    )
    return requisition


# --------------------------------------------------------------------------- #
# PR → PO conversion                                                           #
# --------------------------------------------------------------------------- #

@transaction.atomic
def create_po_from_requisition(requisition, *, vendor, order_date, actor_user=None,
                               currency=None, expected_date=None):
    """Create a :class:`PurchaseOrder` from an **approved** requisition's lines.

    Each requisition line becomes a PO line at its estimated unit price (the buyer can
    edit before issuing). The expense account falls back to the vendor's / vendor
    category's default when a line didn't suggest one.
    """
    from .models import PurchaseOrder, PurchaseOrderLine

    if requisition.status != DocumentStatus.APPROVED:
        raise RequisitionError(
            f"Requisition {requisition.document_number or requisition.pk} must be "
            f"APPROVED before raising a PO (is '{requisition.status}').",
        )

    default_expense = (
        vendor.default_expense_account
        or (vendor.category.default_expense_account if vendor.category_id else None)
    )

    po = PurchaseOrder.objects.create(
        entity=requisition.entity, branch=requisition.branch,
        vendor=vendor, requisition=requisition,
        order_date=order_date, expected_date=expected_date,
        currency=currency, created_by=actor_user,
        narration=requisition.justification,
    )
    for rline in requisition.lines.all().order_by("line_no", "id"):
        expense = rline.expense_account or default_expense
        if expense is None:
            raise RequisitionError(
                f"Requisition line '{rline.description}' has no expense account and the "
                f"vendor has no default — set one before raising the PO.",
            )
        PurchaseOrderLine.objects.create(
            purchase_order=po, requisition_line=rline,
            description=rline.description, expense_account=expense,
            quantity=rline.quantity, unit_price=rline.estimated_unit_price,
            tax_code=rline.tax_code, line_no=rline.line_no,
        )
    price_po(po)
    return po


# --------------------------------------------------------------------------- #
# Goods receipt posting (Dr expense, Cr GR/IR clearing)                        #
# --------------------------------------------------------------------------- #

def post_grn(grn, *, actor_user=None):
    """Post a goods receipt, recognising the cost and parking the GR/IR liability.

    Wrapper that records a durable rejection audit on any :class:`FinanceError`, then
    re-raises — mirroring the journal posting contract.
    """
    try:
        return _post_grn_atomic(grn, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=grn.entity, action=FinanceAuditAction.GRN_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=grn,
        )
        raise


@transaction.atomic
def _post_grn_atomic(grn, *, actor_user=None):
    from vs_finance.models import JournalEntry, JournalLine
    from .models import GoodsReceivedNoteLine, PurchaseOrderLine

    if grn.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"GRN {grn.document_number or grn.pk} is '{grn.status}', only a draft can be posted.",
        )

    # Value each line (accepted_qty × unit_price) and roll up the receipt total.
    expense_by_account: dict[int, int] = defaultdict(int)
    expense_objs: dict[int, object] = {}
    total_value = 0
    lines = list(grn.lines.select_related("expense_account", "po_line").all())
    if not lines:
        raise PostingError("A goods receipt must have at least one line to post.")

    for line in lines:
        value = compute_line_net(line.accepted_qty, line.unit_price)
        if line.value_amount != value:
            GoodsReceivedNoteLine.objects.filter(pk=line.pk).update(value_amount=value)
            line.value_amount = value
        if value <= 0:
            continue
        expense_by_account[line.expense_account_id] += value
        expense_objs[line.expense_account_id] = line.expense_account
        total_value += value

    if total_value <= 0:
        raise PostingError("A goods receipt must have a positive accepted value to post.")

    grir = resolve_account(grn.entity, GRIR_CLEARING_CODE, label="GR/IR clearing")
    period = resolve_period(grn.entity, grn.received_date)

    entry = JournalEntry.objects.create(
        entity=grn.entity, branch=grn.branch,
        date=grn.received_date, period=period,
        source=JournalSource.PURCHASE,
        narration=grn.narration or f"Goods receipt {grn.document_number or ''}".strip(),
        reference=grn.reference, created_by=actor_user,
    )
    line_no = 0
    for acc_id, amount in expense_by_account.items():
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=expense_objs[acc_id], debit=amount, credit=0,
            description="Goods received", line_no=line_no,
        )
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=grir, debit=0, credit=total_value,
        description=f"GR/IR: {grn.vendor.code}", line_no=line_no,
    )

    post_journal(entry, actor_user=actor_user)

    # Advance received quantities on the PO lines.
    for line in lines:
        if line.po_line_id:
            PurchaseOrderLine.objects.filter(pk=line.po_line_id).update(
                received_qty=F("received_qty") + line.accepted_qty,
            )

    grn.journal = entry
    grn.total_value = total_value
    grn.status = DocumentStatus.POSTED
    grn.save(update_fields=["journal", "total_value", "status", "updated_at"])

    record(
        entity=grn.entity, action=FinanceAuditAction.GRN_POSTED,
        actor_user=actor_user, target=grn,
        message=f"Received goods from {grn.vendor.code} ({total_value} kobo to GR/IR).",
        journal_id=entry.pk, value=total_value,
    )
    return grn
