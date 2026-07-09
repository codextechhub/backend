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

# Handle the price vendor invoice workflow.
def price_vendor_invoice(invoice) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the totals."""
    from .models import VendorInvoiceLine

    for line in invoice.lines.all():  # Reprice every bill line from quantity and unit price.
        net = compute_line_net(line.quantity, line.unit_price)  # Compute net kobo exactly.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use the tax rate when a tax code exists.
        tax = compute_tax(net, rate)  # Compute input tax in kobo.
        if line.net_amount != net or line.tax_amount != tax:  # Avoid unnecessary writes.
            VendorInvoiceLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    invoice.recompute_totals(save=True)  # Roll line totals up to the invoice.


# Handle the match vendor invoice workflow.
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

    status = MatchStatus.AUTO_MATCHED  # Default to matched until a variance is found.
    has_po_line = False  # Track whether this bill has any PO-backed lines.

    for line in invoice.lines.select_related("po_line").all():
        if line.po_line_id is None:  # Non-PO lines have nothing to three-way match.
            continue
        has_po_line = True  # At least one line is PO-backed.
        po_line = PurchaseOrderLine.objects.get(pk=line.po_line_id)
        billed_cum = Decimal(po_line.invoiced_qty) + Decimal(line.quantity)
        ordered = Decimal(po_line.quantity)
        received = Decimal(po_line.received_qty)

        if billed_cum > ordered:  # Cannot bill more than ordered.
            status = MatchStatus.OVER_BILLED  # Blocking match status.
            break  # Exit the current loop.
        if billed_cum > received:  # Cannot bill goods that have not been received.
            status = MatchStatus.UNDER_RECEIVED  # Blocking match status.
            break  # Exit the current loop.
        if int(line.unit_price) != int(po_line.unit_price):  # Unit price differs from the PO.
            status = MatchStatus.PRICE_VARIANCE  # Non-blocking variance unless caller disallows it.
            # keep scanning; a later line could be a harder (blocking) failure  # Do not stop on soft variance.

    if not has_po_line:  # Non-PO bills have no match source.
        status = MatchStatus.AUTO_MATCHED  # Treat them as matched.

    invoice.match_status = status  # Store the computed match result on the invoice object.
    if save:  # Persist when the caller wants durable match state.
        invoice.save(update_fields=["match_status", "updated_at"])
    return status  # Return the computed match status.


# --------------------------------------------------------------------------- #
# Vendor invoice posting (Dr GR/IR + input VAT, Cr AP)                         #
# --------------------------------------------------------------------------- #

# Handle the post vendor invoice workflow.
def post_vendor_invoice(invoice, *, actor_user=None, allow_variance=False):
    """Match and post a :class:`VendorInvoice`, raising its AP journal.

    Pricing and the three-way match run **before** the posting transaction and persist
    durably, so a rejected bill still records its computed totals and match outcome
    (the posting itself rolls back). Any :class:`FinanceError` — a blocking match
    failure included — writes a durable rejection audit row, then re-raises.
    """
    if invoice.status == DocumentStatus.DRAFT:  # Draft bills are priced and matched before posting.
        price_vendor_invoice(invoice)  # Ensure bill totals are current.
        match_vendor_invoice(invoice, save=True)  # Persist the match result before attempting the post.
    try:  # The atomic worker owns the GL write; this wrapper owns rejection audit.
        return _post_vendor_invoice_atomic(  # Post the vendor invoice into AP.
            invoice, actor_user=actor_user, allow_variance=allow_variance,
        )
    except FinanceError as exc:  # Log failed posting attempts durably.
        record_rejection(  # Record a vendor invoice post rejection.
            entity=invoice.entity, action=FinanceAuditAction.VENDOR_INVOICE_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise


@transaction.atomic
# Support the post vendor invoice atomic workflow.
def _post_vendor_invoice_atomic(invoice, *, actor_user=None, allow_variance=False):
    from vs_finance.models import JournalEntry, JournalLine
    from .constants import GRIR_CLEARING_CODE
    from .models import PurchaseOrderLine

    if invoice.status != DocumentStatus.DRAFT:  # Only draft bills can be posted.
        raise PostingError(
            f"Vendor invoice {invoice.document_number or invoice.pk} is '{invoice.status}', "
            f"only a draft can be posted.",
        )

    vendor = invoice.vendor  # Vendor drives the AP control account.
    ap_account = vendor.payable_account  # Resolve the vendor payable account.
    if ap_account is None:  # Cannot credit AP without a payable account.
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")

    if invoice.total <= 0:  # Reject zero or negative bills.
        raise PostingError("A vendor invoice must have a positive total to post.")

    match_status = invoice.match_status  # Use the previously computed match result.
    if match_status in MATCH_BLOCKING and not allow_variance:  # Blocking variances stop posting unless explicitly allowed.
        raise ThreeWayMatchError(match_status)

    period = resolve_period(invoice.entity, invoice.invoice_date)  # Find the open accounting period.

    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,
        date=invoice.invoice_date, period=period,
        source=JournalSource.PURCHASE, currency=invoice.currency,
        narration=invoice.narration or f"Bill {invoice.document_number or ''}".strip(),
        reference=invoice.vendor_reference, created_by=actor_user,
    )

    # Debit side: PO-based net clears GR/IR clearing; non-PO net hits the expense
    # account directly. Input tax (recoverable) debits the tax code's paid account.
    grir = None  # Resolve the GR/IR clearing account lazily only when needed.
    debit_by_account: dict[int, int] = defaultdict(int)  # Group net debits by account.
    debit_objs: dict[int, object] = {}  # Keep account objects for grouped debit lines.
    tax_by_account: dict[int, int] = defaultdict(int)  # Group input tax by paid account.
    tax_objs: dict[int, object] = {}  # Keep tax account objects for grouped tax lines.

    for line in invoice.lines.select_related("expense_account", "tax_code__paid_account"):
        if line.po_line_id is not None:  # PO-backed bills clear GR/IR.
            if grir is None:  # Resolve GR/IR once.
                grir = resolve_account(invoice.entity, GRIR_CLEARING_CODE, label="GR/IR clearing")
            target = grir  # Debit GR/IR for PO-backed net amount.
        else:  # Non-PO bills hit the line expense account directly.
            target = line.expense_account
        debit_by_account[target.id] += line.net_amount  # Accumulate the line net amount by account.
        debit_objs[target.id] = target  # Store the account object for journal creation.

        if line.tax_amount:  # Tax-bearing lines require a recoverable tax account.
            tax_acc = line.tax_code.paid_account if line.tax_code_id else None  # Resolve input tax account.
            if tax_acc is None:  # A tax amount without a paid account is invalid.
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no paid (input/recoverable) "
                    f"account set." if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount  # Accumulate tax by account.
            tax_objs[tax_acc.id] = tax_acc  # Store the tax account object.

    line_no = 0  # Track journal line ordering.
    for acc_id, amount in debit_by_account.items():  # Emit grouped net debit lines.
        if amount == 0:  # Skip empty debit groups.
            continue
        line_no += 1  # Advance the journal line counter.
        JournalLine.objects.create(
            entry=entry, account=debit_objs[acc_id], debit=amount, credit=0,
            description="Purchase", line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():  # Emit grouped input tax debit lines.
        line_no += 1  # Advance the journal line counter.
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,
            description="Input tax", line_no=line_no,
        )
    # Credit the AP control for the gross owed.  # Final line records the liability to the vendor.
    line_no += 1  # Advance to the AP credit line.
    JournalLine.objects.create(
        entry=entry, account=ap_account, debit=0, credit=invoice.total,
        description=f"AP: {vendor.code}", line_no=line_no,
    )

    post_journal(entry, actor_user=actor_user)  # Validate and post the balanced AP journal.

    # Advance invoiced quantities on the PO lines.  # Keep procurement quantities in sync with AP posting.
    for line in invoice.lines.all():  # Revisit bill lines after successful posting.
        if line.po_line_id:  # Only PO-backed lines affect PO invoiced quantity.
            PurchaseOrderLine.objects.filter(pk=line.po_line_id).update(
                invoiced_qty=F("invoiced_qty") + line.quantity,
            )

    invoice.journal = entry  # Link the bill to the posted journal.
    invoice.status = DocumentStatus.POSTED  # Mark the bill posted.
    invoice.refresh_payment_status(save=False)  # Recompute payment status.
    invoice.save(update_fields=["journal", "status", "payment_status", "updated_at"])

    record(  # Log the successful vendor invoice post.
        entity=invoice.entity, action=FinanceAuditAction.VENDOR_INVOICE_POSTED,
        actor_user=actor_user, target=invoice,
        message=f"Posted bill from {vendor.code} ({invoice.total} kobo).",
        journal_id=entry.pk, total=invoice.total, tax=invoice.tax_total,
        match_status=str(match_status),
    )
    return invoice  # Return the posted vendor invoice.


# --------------------------------------------------------------------------- #
# Vendor payment posting + allocation (Dr AP, Cr Bank net, Cr WHT)            #
# --------------------------------------------------------------------------- #

# Handle the post vendor payment workflow.
def post_vendor_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    """Post a :class:`VendorPayment` (Dr AP, Cr bank net, Cr WHT) and allocate it.

    ``allocations`` (a list of ``(vendor_invoice, gross_amount_kobo)``) applies an
    explicit split; otherwise ``auto_allocate`` settles the vendor's oldest open bills
    first.
    """
    try:  # The atomic worker owns the GL and allocation work.
        return _post_vendor_payment_atomic(  # Post the payment and optionally allocate it.
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations,
        )
    except FinanceError as exc:  # Log rejected payment posts durably.
        record_rejection(  # Record the failed vendor payment post.
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise


@transaction.atomic
# Support the post vendor payment atomic workflow.
def _post_vendor_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    from vs_finance.models import JournalEntry, JournalLine

    if payment.status != DocumentStatus.DRAFT:  # Only draft vendor payments can be posted.
        raise PostingError(
            f"Vendor payment {payment.document_number or payment.pk} is '{payment.status}', "
            f"only a draft can be posted.",
        )

    vendor = payment.vendor  # Vendor drives AP and blocking rules.
    if vendor.on_hold:  # Payments are blocked for vendors on hold.
        raise PostingError(f"Vendor {vendor.code} is on hold; payments are blocked.")

    ap_account = vendor.payable_account  # Resolve the AP control account.
    if ap_account is None:  # Cannot debit AP without a payable account.
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")
    if payment.payment_account_id is None:  # A bank/cash account is required for the credit side.
        raise PostingError("Vendor payment has no payment (bank/cash) account set.")

    # gross = net + WHT; keep net consistent with the declared gross/WHT.  # Normalize withholding math.
    if payment.gross_amount <= 0:  # Reject zero or negative vendor payments.
        raise PostingError("A vendor payment must have a positive gross amount to post.")
    if payment.wht_amount < 0 or payment.wht_amount > payment.gross_amount:  # WHT cannot exceed gross or go negative.
        raise PostingError("WHT must be between 0 and the gross amount.")
    payment.net_amount = payment.gross_amount - payment.wht_amount  # Recompute net cash paid.

    period = resolve_period(payment.entity, payment.payment_date)  # Find the open accounting period.

    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Vendor payment {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    line_no = 1  # First journal line is the AP debit.
    JournalLine.objects.create(
        entry=entry, account=ap_account, debit=payment.gross_amount, credit=0,
        description=f"AP: {vendor.code}", line_no=line_no,
    )
    line_no += 1  # Second line is the bank/cash credit.
    JournalLine.objects.create(
        entry=entry, account=payment.payment_account, debit=0, credit=payment.net_amount,
        description=f"Payment: {vendor.code}", line_no=line_no,
    )
    if payment.wht_amount:  # Withholding tax creates a payable instead of leaving cash.
        wht_account = (  # Prefer the tax-code account when configured.
            payment.wht_tax_code.collected_account
            if (payment.wht_tax_code_id and payment.wht_tax_code.collected_account_id)  # Branch on the current domain condition.
            else resolve_account(payment.entity, WHT_PAYABLE_CODE, label="WHT payable")
        )
        line_no += 1  # Third line is the WHT payable credit.
        JournalLine.objects.create(
            entry=entry, account=wht_account, debit=0, credit=payment.wht_amount,
            description="WHT withheld", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)  # Validate and post the payment journal.

    payment.journal = entry  # Link the payment to the posted journal.
    payment.status = DocumentStatus.POSTED  # Mark the payment posted.
    payment.save(update_fields=["journal", "net_amount", "status", "updated_at"])

    record(  # Log the successful vendor payment post.
        entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=f"Paid {vendor.code} ({payment.net_amount} kobo net, {payment.wht_amount} WHT).",
        journal_id=entry.pk, gross=payment.gross_amount,
        net=payment.net_amount, wht=payment.wht_amount,
    )

    if allocations:  # Explicit allocations override auto allocation.
        allocate_vendor_payment(payment, allocations=allocations, actor_user=actor_user)  # Apply the explicit plan.
    elif auto_allocate:  # Otherwise settle oldest open bills when enabled.
        allocate_vendor_payment(payment, actor_user=actor_user)  # Auto-allocate against open bills.
    return payment  # Return the posted vendor payment.


@transaction.atomic
# Handle the allocate vendor payment workflow.
def allocate_vendor_payment(payment, *, allocations=None, actor_user=None):
    """Apply a posted vendor payment's unallocated gross to bills.

    ``allocations`` is an optional list of ``(vendor_invoice, gross_amount_kobo)``;
    without it the vendor's open posted bills are settled oldest-first (by due date,
    then invoice date). Never allocates past a bill's balance due or the payment's
    remaining gross. Returns the list of created allocation rows.
    """
    from .models import VendorInvoice, VendorPaymentAllocation

    if payment.status != DocumentStatus.POSTED:  # Only posted payments can be allocated.
        raise PostingError("Only a posted vendor payment can be allocated.")

    remaining = payment.unallocated_amount  # Gross amount left to allocate.
    created = []  # Allocation rows created or extended in this run.

    if allocations is None:  # Build an oldest-first plan when no explicit plan is supplied.
        open_invoices = (  # Posted vendor bills that still have a balance.
            VendorInvoice.objects
            .filter(vendor=payment.vendor, status=DocumentStatus.POSTED)
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .order_by("due_date", "invoice_date", "id")
        )
        plan = [(inv, inv.balance_due) for inv in open_invoices]  # Allocate up to each bill's current balance.
    else:  # Caller supplied an explicit allocation split.
        plan = list(allocations)  # Normalize the iterable to a list.

    for invoice, requested in plan:  # Walk the allocation plan in order.
        if remaining <= 0:  # Stop once the payment is fully allocated.
            break  # Exit the current loop.
        apply_amount = min(int(requested), invoice.balance_due, remaining)  # Cap allocation at requested, bill balance, and remaining payment.
        if apply_amount <= 0:  # Skip zero-value allocations.
            continue
        alloc, _ = VendorPaymentAllocation.objects.get_or_create(
            payment=payment, vendor_invoice=invoice, defaults={"amount": 0},
        )
        alloc.amount += apply_amount  # Increase the allocation amount.
        alloc.save(update_fields=["amount", "updated_at"])

        invoice.amount_paid += apply_amount  # Increase the bill's paid amount.
        invoice.refresh_payment_status(save=False)  # Recompute paid/partial/unpaid state.
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        remaining -= apply_amount  # Reduce the unallocated gross payment amount.
        created.append(alloc)  # Track the allocation row for the return value.

    payment.allocated_amount = payment.gross_amount - remaining  # Store the total gross amount allocated.
    payment.save(update_fields=["allocated_amount", "updated_at"])

    if created:  # Log only when at least one bill was allocated.
        record(  # Write the allocation audit event.
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_ALLOCATED,
            actor_user=actor_user, target=payment,
            message=f"Allocated {payment.allocated_amount} kobo across {len(created)} bill(s).",
            allocated=payment.allocated_amount, unallocated=payment.unallocated_amount,
        )
    return created  # Return allocation rows touched by this call.
