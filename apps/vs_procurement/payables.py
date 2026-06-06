"""Accounts-Payable services — the pay side of Procure-to-Pay.

Mirrors the AR revenue cycle in :mod:`vs_finance.receivables`, but for money *out*:

* **Vendor invoice** → three-way matched, then posted as
  ``Dr GR/IR clearing (+ Dr input VAT), Cr AP control``. For a PO-based bill the debit
  clears the GR/IR liability the goods receipt parked, so once goods are both received
  and billed **GR/IR nets to zero**. A non-PO bill debits the expense directly.
* **Vendor payment** → ``Dr AP (gross), Cr bank (net), Cr WHT payable (withheld)`` —
  then the gross is *allocated* across bills (a sub-ledger act with no further GL).

All amounts are integer kobo; tax/WHT are computed from basis points with the same
``ROUND_HALF_UP`` discipline as the rest of the engine.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from vs_finance.audit import record, record_rejection
from vs_finance.constants import (
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from vs_finance.exceptions import FinanceError, PostingError
from vs_finance.posting import post_journal, resolve_period
from vs_finance.receivables import compute_line_net, compute_tax

from .constants import MATCH_BLOCKING, MatchStatus, WHT_PAYABLE_CODE
from .exceptions import ThreeWayMatchError
from .purchasing import resolve_account


# --------------------------------------------------------------------------- #
# Vendor invoice — pricing + three-way match                                  #
# --------------------------------------------------------------------------- #

def price_vendor_invoice(invoice) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the totals."""
    from .models import VendorInvoiceLine

    for line in invoice.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            VendorInvoiceLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    invoice.recompute_totals(save=True)


def match_vendor_invoice(invoice, *, save: bool = True) -> str:
    """Run the three-way match (PO ↔ GRN ↔ invoice) and return the :class:`MatchStatus`.

    Per line linked to a PO line, compares the cumulative billed quantity and the unit
    price against what was ordered and received:

    * billed beyond the ordered quantity  → ``OVER_BILLED`` (blocking)
    * billed beyond the received quantity  → ``UNDER_RECEIVED`` (blocking)
    * unit price differs from the PO       → ``PRICE_VARIANCE`` (flag, postable)
    * otherwise                            → ``AUTO_MATCHED``

    A bill with no PO linkage is treated as ``AUTO_MATCHED`` (nothing to match against).
    """
    from .models import PurchaseOrderLine

    status = MatchStatus.AUTO_MATCHED
    has_po_line = False

    for line in invoice.lines.select_related("po_line").all():
        if line.po_line_id is None:
            continue
        has_po_line = True
        po_line = PurchaseOrderLine.objects.get(pk=line.po_line_id)  # fresh qtys
        billed_cum = Decimal(po_line.invoiced_qty) + Decimal(line.quantity)
        ordered = Decimal(po_line.quantity)
        received = Decimal(po_line.received_qty)

        if billed_cum > ordered:
            status = MatchStatus.OVER_BILLED
            break
        if billed_cum > received:
            status = MatchStatus.UNDER_RECEIVED
            break
        if int(line.unit_price) != int(po_line.unit_price):
            status = MatchStatus.PRICE_VARIANCE
            # keep scanning; a later line could be a harder (blocking) failure

    if not has_po_line:
        status = MatchStatus.AUTO_MATCHED

    invoice.match_status = status
    if save:
        invoice.save(update_fields=["match_status", "updated_at"])
    return status


# --------------------------------------------------------------------------- #
# Vendor invoice posting (Dr GR/IR + input VAT, Cr AP)                         #
# --------------------------------------------------------------------------- #

def post_vendor_invoice(invoice, *, actor_user=None, allow_variance=False):
    """Match and post a :class:`VendorInvoice`, raising its AP journal.

    Pricing and the three-way match run **before** the posting transaction and persist
    durably, so a rejected bill still records its computed totals and match outcome
    (the posting itself rolls back). Any :class:`FinanceError` — a blocking match
    failure included — writes a durable rejection audit row, then re-raises.
    """
    if invoice.status == DocumentStatus.DRAFT:
        price_vendor_invoice(invoice)
        match_vendor_invoice(invoice, save=True)
    try:
        return _post_vendor_invoice_atomic(
            invoice, actor_user=actor_user, allow_variance=allow_variance,
        )
    except FinanceError as exc:
        record_rejection(
            entity=invoice.entity, action=FinanceAuditAction.VENDOR_INVOICE_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise


@transaction.atomic
def _post_vendor_invoice_atomic(invoice, *, actor_user=None, allow_variance=False):
    from vs_finance.models import JournalEntry, JournalLine
    from .constants import GRIR_CLEARING_CODE
    from .models import PurchaseOrderLine

    if invoice.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Vendor invoice {invoice.document_number or invoice.pk} is '{invoice.status}', "
            f"only a draft can be posted.",
        )

    vendor = invoice.vendor
    ap_account = vendor.payable_account
    if ap_account is None:
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")

    if invoice.total <= 0:
        raise PostingError("A vendor invoice must have a positive total to post.")

    match_status = invoice.match_status
    if match_status in MATCH_BLOCKING and not allow_variance:
        raise ThreeWayMatchError(match_status)

    period = resolve_period(invoice.entity, invoice.invoice_date)

    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,
        date=invoice.invoice_date, period=period,
        source=JournalSource.PURCHASE, currency=invoice.currency,
        narration=invoice.narration or f"Bill {invoice.document_number or ''}".strip(),
        reference=invoice.vendor_reference, created_by=actor_user,
    )

    # Debit side: PO-based net clears GR/IR clearing; non-PO net hits the expense
    # account directly. Input tax (recoverable) debits the tax code's paid account.
    grir = None
    debit_by_account: dict[int, int] = defaultdict(int)
    debit_objs: dict[int, object] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}

    for line in invoice.lines.select_related("expense_account", "tax_code__paid_account"):
        if line.po_line_id is not None:
            if grir is None:
                grir = resolve_account(invoice.entity, GRIR_CLEARING_CODE, label="GR/IR clearing")
            target = grir
        else:
            target = line.expense_account
        debit_by_account[target.id] += line.net_amount
        debit_objs[target.id] = target

        if line.tax_amount:
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None
            if tax_acc is None:
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no paid (input/recoverable) "
                    f"account set." if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    line_no = 0
    for acc_id, amount in debit_by_account.items():
        if amount == 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=debit_objs[acc_id], debit=amount, credit=0,
            description="Purchase", line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,
            description="Input tax", line_no=line_no,
        )
    # Credit the AP control for the gross owed.
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=ap_account, debit=0, credit=invoice.total,
        description=f"AP: {vendor.code}", line_no=line_no,
    )

    post_journal(entry, actor_user=actor_user)

    # Advance invoiced quantities on the PO lines.
    for line in invoice.lines.all():
        if line.po_line_id:
            PurchaseOrderLine.objects.filter(pk=line.po_line_id).update(
                invoiced_qty=F("invoiced_qty") + line.quantity,
            )

    invoice.journal = entry
    invoice.status = DocumentStatus.POSTED
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["journal", "status", "payment_status", "updated_at"])

    record(
        entity=invoice.entity, action=FinanceAuditAction.VENDOR_INVOICE_POSTED,
        actor_user=actor_user, target=invoice,
        message=f"Posted bill from {vendor.code} ({invoice.total} kobo).",
        journal_id=entry.pk, total=invoice.total, tax=invoice.tax_total,
        match_status=str(match_status),
    )
    return invoice


# --------------------------------------------------------------------------- #
# Vendor payment posting + allocation (Dr AP, Cr Bank net, Cr WHT)            #
# --------------------------------------------------------------------------- #

def post_vendor_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    """Post a :class:`VendorPayment` (Dr AP, Cr bank net, Cr WHT) and allocate it.

    ``allocations`` (a list of ``(vendor_invoice, gross_amount_kobo)``) applies an
    explicit split; otherwise ``auto_allocate`` settles the vendor's oldest open bills
    first.
    """
    try:
        return _post_vendor_payment_atomic(
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations,
        )
    except FinanceError as exc:
        record_rejection(
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise


@transaction.atomic
def _post_vendor_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    from vs_finance.models import JournalEntry, JournalLine

    if payment.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Vendor payment {payment.document_number or payment.pk} is '{payment.status}', "
            f"only a draft can be posted.",
        )

    vendor = payment.vendor
    if vendor.on_hold:
        raise PostingError(f"Vendor {vendor.code} is on hold; payments are blocked.")

    ap_account = vendor.payable_account
    if ap_account is None:
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")
    if payment.payment_account_id is None:
        raise PostingError("Vendor payment has no payment (bank/cash) account set.")

    # gross = net + WHT; keep net consistent with the declared gross/WHT.
    if payment.gross_amount <= 0:
        raise PostingError("A vendor payment must have a positive gross amount to post.")
    if payment.wht_amount < 0 or payment.wht_amount > payment.gross_amount:
        raise PostingError("WHT must be between 0 and the gross amount.")
    payment.net_amount = payment.gross_amount - payment.wht_amount

    period = resolve_period(payment.entity, payment.payment_date)

    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Vendor payment {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    line_no = 1
    JournalLine.objects.create(
        entry=entry, account=ap_account, debit=payment.gross_amount, credit=0,
        description=f"AP: {vendor.code}", line_no=line_no,
    )
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=payment.payment_account, debit=0, credit=payment.net_amount,
        description=f"Payment: {vendor.code}", line_no=line_no,
    )
    if payment.wht_amount:
        wht_account = (
            payment.wht_tax_code.collected_account
            if (payment.wht_tax_code_id and payment.wht_tax_code.collected_account_id)
            else resolve_account(payment.entity, WHT_PAYABLE_CODE, label="WHT payable")
        )
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=wht_account, debit=0, credit=payment.wht_amount,
            description="WHT withheld", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)

    payment.journal = entry
    payment.status = DocumentStatus.POSTED
    payment.save(update_fields=["journal", "net_amount", "status", "updated_at"])

    record(
        entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=f"Paid {vendor.code} ({payment.net_amount} kobo net, {payment.wht_amount} WHT).",
        journal_id=entry.pk, gross=payment.gross_amount,
        net=payment.net_amount, wht=payment.wht_amount,
    )

    if allocations:
        allocate_vendor_payment(payment, allocations=allocations, actor_user=actor_user)
    elif auto_allocate:
        allocate_vendor_payment(payment, actor_user=actor_user)
    return payment


@transaction.atomic
def allocate_vendor_payment(payment, *, allocations=None, actor_user=None):
    """Apply a posted vendor payment's unallocated gross to bills.

    ``allocations`` is an optional list of ``(vendor_invoice, gross_amount_kobo)``;
    without it the vendor's open posted bills are settled oldest-first (by due date,
    then invoice date). Never allocates past a bill's balance due or the payment's
    remaining gross. Returns the list of created allocation rows.
    """
    from .models import VendorInvoice, VendorPaymentAllocation

    if payment.status != DocumentStatus.POSTED:
        raise PostingError("Only a posted vendor payment can be allocated.")

    remaining = payment.unallocated_amount
    created = []

    if allocations is None:
        open_invoices = (
            VendorInvoice.objects
            .filter(vendor=payment.vendor, status=DocumentStatus.POSTED)
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .order_by("due_date", "invoice_date", "id")
        )
        plan = [(inv, inv.balance_due) for inv in open_invoices]
    else:
        plan = list(allocations)

    for invoice, requested in plan:
        if remaining <= 0:
            break
        apply_amount = min(int(requested), invoice.balance_due, remaining)
        if apply_amount <= 0:
            continue
        alloc, _ = VendorPaymentAllocation.objects.get_or_create(
            payment=payment, vendor_invoice=invoice, defaults={"amount": 0},
        )
        alloc.amount += apply_amount
        alloc.save(update_fields=["amount", "updated_at"])

        invoice.amount_paid += apply_amount
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        remaining -= apply_amount
        created.append(alloc)

    payment.allocated_amount = payment.gross_amount - remaining
    payment.save(update_fields=["allocated_amount", "updated_at"])

    if created:
        record(
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_ALLOCATED,
            actor_user=actor_user, target=payment,
            message=f"Allocated {payment.allocated_amount} kobo across {len(created)} bill(s).",
            allocated=payment.allocated_amount, unallocated=payment.unallocated_amount,
        )
    return created
