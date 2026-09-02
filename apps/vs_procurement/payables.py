"""Accounts-Payable services - the pay side of Procure-to-Pay.

Mirrors the AR revenue cycle in :mod:`vs_finance.receivables`, but for money *out*:

* **Vendor invoice** → three-way matched, then posted as
  ``Dr GR/IR clearing (+ Dr input VAT), Cr AP control``. For a PO-based bill the debit
  clears the GR/IR liability the goods receipt parked, so once goods are both received
  and billed **GR/IR nets to zero**. A non-PO bill debits the expense directly.
* **Vendor payment** → ``Dr AP (settled), Dr vendor advances (unsettled),
  Cr bank (net), Cr WHT payable (withheld)``. The debit is **split at source**: AP is
  debited only for what the payment actually settles, and anything paid ahead of a bill
  lands in the vendor-advance asset. Debiting AP for the whole gross would put a debit
  balance on a liability, which reads as "our suppliers owe us money"; the truth is that
  we are out of pocket and the vendor owes us goods, which is an asset. Applying that
  advance to a bill later reclassifies it (``Dr AP, Cr vendor advances``) on the later of
  the two documents' dates.

The AR side solves the same problem in the opposite direction (see
:func:`vs_finance.receivables._post_payment_atomic`): cash received early is a
*liability*, customer credit, because the customer's money is still theirs. A vendor
advance is its mirror, not its copy.

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
    AccountType,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from vs_finance.exceptions import FinanceError, PostingError
from vs_finance.posting import post_journal, resolve_period
from vs_finance.money import format_naira
from vs_finance.receivables import (
    compute_line_net, compute_tax, stamp_allocation_effective_date,
)

from .constants import (
    MATCH_BLOCKING, PURCHASE_PRICE_VARIANCE_CODE, MatchStatus, ProcApprovalState,
    VENDOR_ADVANCE_CODE, VendorKycStatus, WHT_PAYABLE_CODE,
)
from .exceptions import ThreeWayMatchError
from .purchasing import resolve_account
from .settings import resolve_procurement_settings


# --------------------------------------------------------------------------- #
# Vendor invoice - pricing + three-way match                                  #
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

    * billed beyond the configured ordered tolerance → ``OVER_BILLED`` (blocking)
    * billed beyond the configured receipt tolerance → ``UNDER_RECEIVED`` (blocking)
    * price variance outside tolerance                  → ``PRICE_VARIANCE``
    * otherwise                                         → ``AUTO_MATCHED``

    A bill with no PO linkage follows the entity's explicit non-PO invoice policy.
    """
    policy = resolve_procurement_settings(invoice.entity)
    quantity_factor = Decimal(10000 + policy.quantity_tolerance_bps) / Decimal(10000)
    status = MatchStatus.AUTO_MATCHED  # Default to matched until a variance is found.
    has_po_line = False  # Track whether this bill has any PO-backed lines.
    billed_by_po_line: dict[int, Decimal] = defaultdict(Decimal)
    po_lines = {}
    price_variance = False

    for line in invoice.lines.select_related("po_line").all():
        if line.po_line_id is None:  # Non-PO lines have nothing to three-way match.
            continue
        has_po_line = True  # At least one line is PO-backed.
        po_line = line.po_line
        po_lines[po_line.pk] = po_line
        # Several invoice rows may point at one PO row; aggregate them before
        # comparing so splitting a quantity cannot bypass the ordered/received cap.
        billed_by_po_line[po_line.pk] += Decimal(line.quantity)
        expected_price = abs(int(po_line.unit_price))
        price_delta = abs(int(line.unit_price) - int(po_line.unit_price))
        price_outside_tolerance = (
            price_delta > 0 if expected_price == 0
            else price_delta * 10000 > expected_price * policy.price_tolerance_bps
        )
        if price_outside_tolerance:
            price_variance = True

    for po_line_id, current_qty in billed_by_po_line.items():
        po_line = po_lines[po_line_id]
        billed_cum = Decimal(po_line.invoiced_qty) + current_qty
        ordered = Decimal(po_line.quantity)
        received = Decimal(po_line.received_qty)

        if billed_cum > ordered * quantity_factor:  # Apply the configured quantity tolerance.
            status = MatchStatus.OVER_BILLED  # Blocking match status.
            break  # Exit the current loop.
        if billed_cum > received * quantity_factor:  # Apply the configured receipt tolerance.
            status = MatchStatus.UNDER_RECEIVED  # Blocking match status.
            break  # Exit the current loop.

    if status == MatchStatus.AUTO_MATCHED and price_variance:
        status = MatchStatus.PRICE_VARIANCE  # Exact-price policy: any difference is visible but non-blocking.

    if not has_po_line:  # Non-PO bills follow the entity's explicit policy.
        status = (
            MatchStatus.AUTO_MATCHED
            if policy.allow_non_po_invoices
            else MatchStatus.NON_PO_BLOCKED
        )

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
    (the posting itself rolls back). Any :class:`FinanceError` - a blocking match
    failure included - writes a durable rejection audit row, then re-raises.
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
    """Revalidate and post one approved bill under invoice/PO-line locks.

    Lock order is invoice first, then referenced PO lines in primary-key order. Repricing
    and matching happen again under those locks; only after the journal posts are the PO
    invoiced counters and invoice status advanced in the same transaction.
    """
    from vs_finance.models import JournalEntry, JournalLine
    from .constants import GRIR_CLEARING_CODE
    from .models import PurchaseOrderLine, VendorInvoice

    # Lock the invoice and every referenced PO row before re-running the match.
    # This prevents two concurrent bills from both observing the same un-invoiced
    # quantity and posting beyond what was ordered or received.
    invoice = VendorInvoice.objects.select_for_update().select_related("vendor").get(pk=invoice.pk)
    po_line_ids = list(invoice.lines.exclude(po_line_id=None).values_list("po_line_id", flat=True))
    if po_line_ids:
        list(PurchaseOrderLine.objects.select_for_update().filter(pk__in=po_line_ids).order_by("pk"))

    if invoice.status != DocumentStatus.DRAFT:  # Only draft bills can be posted.
        raise PostingError(
            f"Vendor invoice {invoice.document_number or invoice.pk} is '{invoice.status}', "
            f"only a draft can be posted.",
        )
    if invoice.approval_state != ProcApprovalState.APPROVED:
        raise PostingError(
            f"Vendor invoice {invoice.document_number or invoice.pk} must be approved before posting."
        )

    # Pricing and matching are repeated under the row locks because approval may
    # have taken time and another invoice could have consumed PO quantities since.
    price_vendor_invoice(invoice)
    match_vendor_invoice(invoice, save=True)

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

    # Receipt/PO-backed net clears GR/IR at its historical basis; any difference
    # between that basis and the supplier's actual invoice price lands in PPV.
    # Truly direct bills still debit expense. Tax remains based on invoice actual.
    grir = None  # Resolve the GR/IR clearing account lazily only when needed.
    grir_basis_total = 0
    debit_by_account: dict[tuple[int, int | None], int] = defaultdict(int)
    debit_objs: dict[int, object] = {}  # Keep account objects for grouped debit lines.
    ppv = None
    ppv_by_cost_center: dict[int | None, int] = defaultdict(int)
    tax_by_account: dict[int, int] = defaultdict(int)  # Group input tax by paid account.
    tax_objs: dict[int, object] = {}  # Keep tax account objects for grouped tax lines.

    for line in invoice.lines.select_related(
        "expense_account", "cost_center", "po_line",
        "tax_code__paid_account", "grn_line__grn",
    ):
        receipt_backed = (
            line.grn_line_id is not None
            and line.grn_line.grn.status == DocumentStatus.POSTED
        )
        if line.po_line_id is not None or receipt_backed:
            if grir is None:  # Resolve GR/IR once.
                grir = resolve_account(invoice.entity, GRIR_CLEARING_CODE, label="GR/IR clearing")
            basis_unit_price = (
                line.grn_line.unit_price if receipt_backed
                else line.po_line.unit_price
            )
            basis = compute_line_net(line.quantity, basis_unit_price)
            grir_basis_total += basis
            variance = line.net_amount - basis
            if variance:
                if ppv is None:
                    ppv = resolve_account(
                        invoice.entity, PURCHASE_PRICE_VARIANCE_CODE,
                        label="purchase price variance",
                    )
                ppv_by_cost_center[line.cost_center_id] += variance
        else:  # Non-PO bills hit the line expense account directly.
            target = line.expense_account
            debit_by_account[(target.id, line.cost_center_id)] += line.net_amount
            debit_objs[target.id] = target

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
    if grir is not None and grir_basis_total:
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=grir, debit=grir_basis_total, credit=0,
            description="GR/IR clearing", line_no=line_no,
        )
    for (acc_id, cost_center_id), amount in debit_by_account.items():
        if amount == 0:  # Skip empty debit groups.
            continue
        line_no += 1  # Advance the journal line counter.
        JournalLine.objects.create(
            entry=entry, account=debit_objs[acc_id], debit=amount, credit=0,
            cost_center_id=cost_center_id, description="Purchase", line_no=line_no,
        )
    for cost_center_id, variance in ppv_by_cost_center.items():
        if variance == 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=ppv,
            debit=variance if variance > 0 else 0,
            credit=-variance if variance < 0 else 0,
            cost_center_id=cost_center_id,
            description="Purchase price variance", line_no=line_no,
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
        message=f"Posted bill from {vendor.code} ({format_naira(invoice.total)}).",
        journal_id=entry.pk, total=invoice.total, tax=invoice.tax_total,
        match_status=str(match_status), allow_variance=bool(allow_variance),
        variance_override_used=bool(allow_variance and match_status in MATCH_BLOCKING),
    )
    return invoice  # Return the posted vendor invoice.


# --------------------------------------------------------------------------- #
# Vendor payment posting + allocation (Dr AP, Cr Bank net, Cr WHT)            #
# --------------------------------------------------------------------------- #

# Handle the post vendor payment workflow.
def post_vendor_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                        system_originated=False):
    """Post a :class:`VendorPayment` (Dr AP/vendor advances, Cr bank net, Cr WHT).

    ``allocations`` (a list of ``(vendor_invoice, gross_amount_kobo)``) applies an
    explicit split; otherwise ``auto_allocate`` settles the vendor's oldest open bills
    first.

    ``system_originated`` marks a post that records an already-completed disbursement
    (e.g. booking a gateway payout that has paid). Such posts skip the pre-disbursement
    governance/eligibility gates - those belong upstream, at payout initiation - while
    every ledger-integrity check still applies.
    """
    try:  # The atomic worker owns the GL and allocation work.
        return _post_vendor_payment_atomic(  # Post the payment and optionally allocate it.
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations,
            system_originated=system_originated,
        )
    except FinanceError as exc:  # Log rejected payment posts durably.
        record_rejection(  # Record the failed vendor payment post.
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POST_REJECTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise


@transaction.atomic
# Support the post vendor payment atomic workflow.
def _post_vendor_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                                system_originated=False):
    """Post one payment and turn its approved allocation plan into settlements atomically.

    Locks are acquired in stable domain order: payment, vendor, persisted plan rows, then
    invoices (explicit targets by primary key; automatic targets in due/invoice/id
    settlement order). Draft allocation rows are approval instructions; they are deleted
    and only rows written by the settlement pass represent posted sub-ledger settlement.

    The settlement pass runs **before** the journal, because what it settles is what
    decides the journal's debit split: AP is debited for the settled part only, and the
    rest is a vendor advance. Both live in this one transaction, so the sub-ledger and
    the GL cannot disagree about how the money was classified.
    """
    from vs_finance.models import JournalEntry, JournalLine
    from .models import Vendor, VendorInvoice, VendorPayment, VendorPaymentAllocation

    # Lock the payment first, then invoice rows in stable primary-key order. This
    # prevents duplicate journals and two payments consuming the same bill balance.
    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related(
        "vendor", "payment_account", "wht_tax_code__collected_account",
    ).get(pk=payment.pk)
    # Lock only the master row before rechecking eligibility. The payable-account
    # relation is nullable, and PostgreSQL rejects FOR UPDATE across that outer join.
    payment.vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=payment.vendor_id)

    persisted_plan = list(
        VendorPaymentAllocation.objects.select_for_update().filter(payment=payment)
        .select_related("vendor_invoice").order_by("vendor_invoice_id")
    )
    if allocations is None and persisted_plan:
        allocations = [(row.vendor_invoice, row.amount) for row in persisted_plan]

    explicit_ids = [invoice.pk for invoice, _ in allocations] if allocations else []
    if explicit_ids:
        locked = {
            invoice.pk: invoice for invoice in VendorInvoice.objects.select_for_update()
            .filter(pk__in=explicit_ids).order_by("pk")
        }
        allocations = [(locked.get(invoice.pk, invoice), amount) for invoice, amount in allocations]
    elif auto_allocate:
        # Auto-allocation also locks every candidate before the journal is written.
        list(
            VendorInvoice.objects.select_for_update().filter(
                entity=payment.entity, vendor=payment.vendor, status=DocumentStatus.POSTED,
            ).exclude(payment_status=InvoicePaymentStatus.PAID).order_by("due_date", "invoice_date", "id")
        )

    if payment.status != DocumentStatus.DRAFT:  # Only draft vendor payments can be posted.
        raise PostingError(
            f"Vendor payment {payment.document_number or payment.pk} is '{payment.status}', "
            f"only a draft can be posted.",
        )
    vendor = payment.vendor  # Vendor drives AP and blocking rules.
    # Pre-disbursement governance + vendor-eligibility gates. A system-originated
    # post records a disbursement that has already happened (e.g. a gateway
    # payout), so it skips these - refusing the ledger entry after the money has
    # left would strand the payment. Eligibility is enforced upstream at payout
    # initiation. Ledger-integrity checks below still apply either way.
    if not system_originated:
        if payment.approval_state != ProcApprovalState.APPROVED:
            raise PostingError(
                f"Vendor payment {payment.document_number or payment.pk} must be approved before posting."
            )
        if not vendor.is_active:
            raise PostingError(f"Vendor {vendor.code} is inactive; payments are blocked.")
        if vendor.kyc_status != VendorKycStatus.VERIFIED:
            raise PostingError(f"Vendor {vendor.code} must be KYC verified before payment.")
        if vendor.on_hold:  # Payments are blocked for vendors on hold.
            raise PostingError(f"Vendor {vendor.code} is on hold; payments are blocked.")

    ap_account = vendor.payable_account  # Resolve the AP control account.
    if ap_account is None:  # Cannot debit AP without a payable account.
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")
    if payment.payment_account_id is None:  # A bank/cash account is required for the credit side.
        raise PostingError("Vendor payment has no payment (bank/cash) account set.")
    if (
        payment.payment_account.account_type != AccountType.ASSET
        or not payment.payment_account.is_active
        or not payment.payment_account.is_postable
    ):
        raise PostingError("Vendor payment account must be an active, postable asset account.")

    # gross = net + WHT; keep net consistent with the declared gross/WHT.  # Normalize withholding math.
    if payment.gross_amount <= 0:  # Reject zero or negative vendor payments.
        raise PostingError("A vendor payment must have a positive gross amount to post.")
    if payment.wht_amount < 0 or payment.wht_amount > payment.gross_amount:  # WHT cannot exceed gross or go negative.
        raise PostingError("WHT must be between 0 and the gross amount.")
    payment.net_amount = payment.gross_amount - payment.wht_amount  # Recompute net cash paid.

    # Draft allocation rows are approval instructions, not settled sub-ledger rows.
    # They are replaced by the settlement pass below, which writes the real ones.
    if persisted_plan:
        VendorPaymentAllocation.objects.filter(payment=payment).delete()

    # Settle first, then journal. ``as_of`` is the payment's own date: this journal is
    # dated there, so it cannot debit AP for a bill that does not exist yet. Such a bill
    # is skipped (auto) or refused (named), and the money falls through to the vendor
    # advance - which is exactly what it is.
    plan = (
        _build_vendor_bill_plan(payment, allocations, as_of=payment.payment_date)
        if (allocations is not None or auto_allocate) else []
    )
    settled, created_rows, _latest = _apply_vendor_payment_subledger(
        payment, plan, remaining=payment.gross_amount, strict=bool(allocations),
    )
    advance = payment.gross_amount - settled  # Paid ahead of any bill: an asset, not AP.
    # Every row this pass wrote is debited to AP by the journal below, dated
    # payment_date; the plan was filtered to bills already raised by then.
    stamp_allocation_effective_date(created_rows, payment.payment_date)

    period = resolve_period(payment.entity, payment.payment_date)  # Find the open accounting period.

    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Vendor payment {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    line_no = 0  # Track journal line ordering.
    if settled > 0:  # Only the part that actually settles a bill reduces the liability.
        line_no += 1  # First journal line is the AP debit.
        JournalLine.objects.create(
            entry=entry, account=ap_account, debit=settled, credit=0,
            description=f"AP: {vendor.code}", line_no=line_no,
        )
    if advance > 0:  # The rest is money the vendor owes us in goods.
        line_no += 1  # Next line is the vendor-advance asset debit.
        JournalLine.objects.create(
            entry=entry,
            account=resolve_account(
                payment.entity, VENDOR_ADVANCE_CODE, label="vendor advances",
            ),
            debit=advance, credit=0,
            description=f"Vendor advance: {vendor.code}", line_no=line_no,
        )
    line_no += 1  # Then the bank/cash credit.
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
        line_no += 1  # Last line is the WHT payable credit.
        JournalLine.objects.create(
            entry=entry, account=wht_account, debit=0, credit=payment.wht_amount,
            description="WHT withheld", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)  # Validate and post the payment journal.

    payment.journal = entry  # Link the payment to the posted journal.
    payment.status = DocumentStatus.POSTED  # Mark the payment posted.
    payment.allocated_amount = settled  # Store the gross actually applied to bills.
    payment.save(update_fields=[
        "journal", "net_amount", "status", "allocated_amount", "updated_at",
    ])

    record(  # Log the successful vendor payment post.
        entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=(
            f"Paid {vendor.code} ({format_naira(payment.net_amount)} net, "
            f"{format_naira(payment.wht_amount)} WHT)."
        ),
        journal_id=entry.pk, gross=payment.gross_amount,
        net=payment.net_amount, wht=payment.wht_amount,
        allocated=settled, advance=advance,
    )
    if created_rows:  # Keep the payment's activity feed reading as it always has.
        record(  # Log the settlement the posting journal carried out.
            entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_ALLOCATED,
            actor_user=actor_user, target=payment,
            message=f"Allocated {format_naira(settled)} across {len(created_rows)} bill(s).",
            journal_id=entry.pk, allocated=settled, unallocated=advance,
            effective_date=str(payment.payment_date),
        )
    return payment  # Return the posted vendor payment.


# Build the list of bills a vendor payment should settle, in settlement order.
def _build_vendor_bill_plan(payment, allocations, *, as_of=None):
    """An explicit ``[(bill, amount)]`` plan, or the vendor's open bills oldest-first.

    ``as_of`` is the settling journal's own accounting date, and it is the choke point
    for causal ordering: a payment journal dated 1 March cannot debit AP for a bill
    raised on 10 March, because on 1 March that liability does not exist and the debit
    would drive the control account negative for the gap.

    The two modes handle that differently, on purpose, exactly as
    :func:`vs_finance.receivables._build_invoice_plan` does:

    * **Auto-allocation** silently *skips* a not-yet-raised bill. That is not a
      failure - it is a prepayment, and the money correctly falls through to the
      vendor-advance asset to be applied when the bill arrives.
    * **An explicit plan** names a bill the user chose, so silently dropping it would
      post something other than what was asked for. It raises, and says which date to use.

    Pass no ``as_of`` when the settlement raises its own journal dated at the later of
    the two documents (see :func:`allocate_vendor_payment`); applying an existing advance
    to a newer bill is ordinary business and must not be refused.
    """
    from vs_finance.chronology import accounting_date, describe, ensure_on_or_after

    from .models import VendorInvoice

    if allocations is not None:  # Explicit allocations always win over auto-allocation.
        plan = list(allocations)  # Normalize the iterable to a list.
        if as_of is not None:  # A named bill must already exist on the settling date.
            for invoice, _requested in plan:
                bill_date = accounting_date(invoice)
                ensure_on_or_after(
                    subject=f"Vendor payment {payment.document_number or payment.pk}",
                    subject_date=as_of,
                    source=f"bill {describe(invoice, 'the vendor invoice')}",
                    source_date=bill_date,
                    remedy=(
                        f"Either date the payment {bill_date} or later, or leave it "
                        f"unallocated and apply it once the bill is raised."
                    ),
                )
        return plan  # Explicit plan passed its date checks.

    open_invoices = (  # Posted vendor bills that still have a balance.
        VendorInvoice.objects
        .filter(vendor=payment.vendor, status=DocumentStatus.POSTED)
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .order_by("due_date", "invoice_date", "id")
    )
    if as_of is not None:  # Auto-allocation only settles what already exists.
        open_invoices = open_invoices.filter(invoice_date__lte=as_of)
    return [(inv, inv.balance_due) for inv in open_invoices]  # Up to each bill's balance.


# Apply a settlement plan to the AP sub-ledger, without touching the GL.
def _apply_vendor_payment_subledger(payment, plan, *, remaining, strict=False):
    """Settle ``plan``'s bills from ``remaining`` kobo, capped at each bill's balance.

    GL-agnostic by design: the caller owns the journal, because the *same* settlement
    means a different journal depending on where the money currently sits. At posting
    time the settled total is debited straight to AP; later, it is a reclassification
    out of the vendor advance. Splitting the sub-ledger work out is what lets both
    callers share one set of validation rules.

    Returns ``(applied, created_rows, latest_bill_date)``. The last value is the newest
    bill date this run actually settled, which is what a later caller needs to date its
    reclassification journal so AP is never debited before the liability exists.
    """
    from vs_finance.chronology import accounting_date

    from .models import VendorPaymentAllocation

    created = []  # Allocation rows written by this run.
    applied = 0  # Total gross settled by this run.
    latest = None  # Newest bill date this run actually settled.

    # The same read-modify-write race the AR side has, on the money-out leg: two
    # payments settling one bill both read its pre-update ``amount_paid``, both
    # write their own total back, and the bill records one settlement while AP is
    # debited twice. Locked through the shared helper so the two ledgers cannot
    # drift apart in how they answer it. See
    # :func:`vs_finance.receivables.lock_settlement_targets`.
    from vs_finance.receivables import lock_settlement_targets
    plan = lock_settlement_targets(plan)

    seen_invoice_ids = set()
    if strict:
        planned_total = 0
        for invoice, requested in plan:
            requested = int(requested)
            if invoice.pk in seen_invoice_ids:
                raise PostingError("A vendor invoice may appear only once in a payment allocation plan.")
            seen_invoice_ids.add(invoice.pk)
            if invoice.entity_id != payment.entity_id or invoice.vendor_id != payment.vendor_id:
                raise PostingError("Every allocated invoice must belong to the payment entity and vendor.")
            if invoice.status != DocumentStatus.POSTED:
                raise PostingError(f"Vendor invoice {invoice.document_number or invoice.pk} is not posted.")
            if requested <= 0:
                raise PostingError("Allocation amounts must be greater than zero.")
            if requested > invoice.balance_due:
                raise PostingError(
                    f"Allocation for {invoice.document_number or invoice.pk} exceeds its current balance."
                )
            planned_total += requested
        if planned_total > remaining:
            raise PostingError("Allocation plan exceeds the payment gross amount.")

    seen_invoice_ids.clear()
    for invoice, requested in plan:  # Walk the allocation plan in order.
        if remaining <= 0:  # Stop once the payment is fully allocated.
            break  # Exit the current loop.
        requested = int(requested)
        if invoice.pk in seen_invoice_ids:
            raise PostingError("A vendor invoice may appear only once in a payment allocation plan.")
        seen_invoice_ids.add(invoice.pk)
        if invoice.entity_id != payment.entity_id or invoice.vendor_id != payment.vendor_id:
            raise PostingError("Every allocated invoice must belong to the payment entity and vendor.")
        if invoice.status != DocumentStatus.POSTED:
            raise PostingError(f"Vendor invoice {invoice.document_number or invoice.pk} is not posted.")
        if requested <= 0:
            raise PostingError("Allocation amounts must be greater than zero.")
        apply_amount = min(int(requested), invoice.balance_due, remaining)  # Cap allocation at requested, bill balance, and remaining payment.
        if apply_amount <= 0:  # Skip zero-value allocations.
            continue
        # One row per settlement event, never a running total. The caller stamps each
        # row with the date of the journal that debited AP for it, and a second tranche
        # against the same bill can move AP on a different date - merging them would
        # leave one row that cannot honestly carry either date.
        alloc = VendorPaymentAllocation.objects.create(
            payment=payment, vendor_invoice=invoice, amount=apply_amount,
        )

        invoice.amount_paid += apply_amount  # Increase the bill's paid amount.
        invoice.refresh_payment_status(save=False)  # Recompute paid/partial/unpaid state.
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        remaining -= apply_amount  # Reduce the money still available to settle with.
        applied += apply_amount  # Track the total settled by this run.
        created.append(alloc)  # Track the allocation row for the return value.
        bill_date = accounting_date(invoice)  # Date of the bill just settled.
        if bill_date is not None and (latest is None or bill_date > latest):
            latest = bill_date  # Track the newest settled bill date.
    return applied, created, latest  # Settled amount, allocation rows, newest bill date.


@transaction.atomic
# Handle the allocate vendor payment workflow.
def allocate_vendor_payment(payment, *, allocations=None, actor_user=None, strict=False):
    """Apply a posted payment's **vendor advance** to bills, reclassifying it into AP.

    After posting, anything the payment did not settle sits in the vendor-advance asset
    (1240). Applying it to a bill moves it back where the settlement belongs
    (``Dr AP, Cr vendor advances``) and settles the bill - no cash moves.
    ``allocations`` is an optional explicit ``[(bill, amount)]`` plan; without it the
    vendor's open posted bills are settled oldest-first (by due date, then invoice date).
    Never allocates past a bill's balance due or the advance still remaining.

    Applying an older payment to a newer bill is ordinary and allowed - that is a
    prepayment finding its bill, and the whole reason the advance account exists. What
    is *not* allowed is dating that reclassification on the payment's date when the bill
    is newer, which would debit AP before the liability existed; the journal is dated at
    the later of the two instead. The AP mirror of
    :func:`vs_finance.receivables.allocate_payment`.
    """
    from vs_finance.chronology import effective_allocation_date
    from vs_finance.models import JournalEntry, JournalLine

    from .models import Vendor, VendorAdvanceAllocationJournal, VendorPayment

    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related(
        "vendor",
    ).get(pk=payment.pk)
    # Lock only the master row: the payable-account relation is nullable and PostgreSQL
    # rejects FOR UPDATE across that outer join.
    payment.vendor = Vendor.objects.select_for_update(of=("self",)).get(pk=payment.vendor_id)

    if payment.status != DocumentStatus.POSTED:  # Only posted payments can be allocated.
        raise PostingError("Only a posted vendor payment can be allocated.")

    vendor = payment.vendor  # Vendor drives the AP control account.
    ap_account = vendor.payable_account  # Resolve the AP control account.
    if ap_account is None:  # Cannot debit AP without a payable account.
        raise PostingError(f"Vendor {vendor.code} has no payable (AP control) account set.")

    remaining = payment.advance_remaining  # Money of this payment still in 1240.
    if remaining <= 0:  # Nothing sitting in the advance to apply.
        return []

    plan = _build_vendor_bill_plan(payment, allocations)  # No cutoff: a newer bill is fine.
    applied, created, latest = _apply_vendor_payment_subledger(
        payment, plan, remaining=remaining, strict=strict,
    )
    if applied <= 0:  # No bill was eligible for allocation.
        return []

    effective = effective_allocation_date(payment.payment_date, [latest])  # Later of the two.
    stamp_allocation_effective_date(created, effective)  # Rows carry the date AP moved.
    period = resolve_period(payment.entity, effective)  # Find the open accounting period.
    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=effective, period=period,
        source=JournalSource.PURCHASE, currency=payment.currency,
        narration=f"Apply vendor advance: {vendor.code}",
        reference=payment.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=ap_account, debit=applied, credit=0,
        description=f"AP: {vendor.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry,
        account=resolve_account(payment.entity, VENDOR_ADVANCE_CODE, label="vendor advances"),
        debit=0, credit=applied,
        description=f"Vendor advance applied: {vendor.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post the reclassification.

    # Attach the journal to the payment durably, so a reversal can unwind every GL
    # effect the payment owns and not just its original disbursement.
    VendorAdvanceAllocationJournal.objects.create(
        payment=payment, journal=entry, amount=applied,
    )

    payment.allocated_amount += applied  # Increase the payment's applied total.
    payment.save(update_fields=["allocated_amount", "updated_at"])

    record(  # Write the allocation audit event.
        entity=payment.entity, action=FinanceAuditAction.VENDOR_PAYMENT_ALLOCATED,
        actor_user=actor_user, target=payment,
        message=f"Allocated {format_naira(applied)} of vendor advance across {len(created)} bill(s).",
        journal_id=entry.pk, allocated=payment.allocated_amount,
        unallocated=payment.advance_remaining, effective_date=str(effective),
    )
    return created  # Return allocation rows created by this call.


# Refuse a reversal whose later advance draw-downs are not fully linked to journals.
def _ensure_advance_journal_coverage(payment, links) -> None:
    """Check every kobo settled after posting has a reclassification journal attached.

    What the posting journal debited to AP is settlement the payment journal itself
    carries; anything allocated beyond that was drawn out of the vendor advance later
    and must have its own linked journal. If the two disagree, some GL effect of this
    payment is unreachable from the payment, and reversing only the part we can see
    would desynchronise the ledger. Refuse instead, and say so.
    """
    ap_account_id = payment.vendor.payable_account_id
    initially_settled = sum(
        int(value or 0) for value in payment.journal.lines
        .filter(account_id=ap_account_id).values_list("debit", flat=True)
    )
    expected_later = max(0, int(payment.allocated_amount) - initially_settled)
    linked_later = sum(int(link.amount) for link in links)
    if linked_later != expected_later:
        raise PostingError(
            f"Vendor payment {payment.document_number or payment.pk} has {expected_later} kobo "
            f"of later vendor-advance allocations but only {linked_later} kobo of linked "
            "reclassification journals. Repair the allocation-journal links before "
            "reversing; reversing only part would desynchronise the ledger.",
        )


@transaction.atomic
def reverse_vendor_payment(payment, *, actor_user=None, date=None):
    """Reverse a posted payment and restore every invoice settlement it funded.

    Lock order mirrors posting: payment, allocation rows by invoice id, then invoices by
    primary key. The reversal journals restore the GL; allocation rows remain as history
    while invoice ``amount_paid`` and derived payment status are rolled back.

    A payment that later applied its vendor advance to a bill owns **more than one**
    journal: the original disbursement plus one reclassification per draw-down. All of
    them are reversed, newest first. Reversing only the disbursement would strip the
    Cr bank / Dr advance while leaving the Dr AP / Cr advance behind, pushing the advance
    account credit-negative and understating AP by the same amount.
    """
    from vs_finance.posting import reverse_journal
    from .models import (
        VendorAdvanceAllocationJournal, VendorInvoice, VendorPayment, VendorPaymentAllocation,
    )

    payment = VendorPayment.objects.select_for_update(of=("self",)).select_related(
        "journal", "vendor",
    ).get(pk=payment.pk)
    if payment.status != DocumentStatus.POSTED or payment.journal_id is None:
        raise PostingError("Only a posted vendor payment with a journal can be reversed.")

    allocations = list(
        VendorPaymentAllocation.objects.select_for_update().filter(payment=payment)
        .select_related("vendor_invoice").order_by("vendor_invoice_id")
    )
    invoice_ids = [allocation.vendor_invoice_id for allocation in allocations]
    locked_invoices = {
        invoice.pk: invoice for invoice in VendorInvoice.objects.select_for_update()
        .filter(pk__in=invoice_ids).order_by("pk")
    }
    advance_journals = list(
        VendorAdvanceAllocationJournal.objects.select_for_update()
        .filter(payment=payment).select_related("journal")
        .order_by("journal__date", "journal_id")
    )
    _ensure_advance_journal_coverage(payment, advance_journals)

    for link in reversed(advance_journals):  # Newest reclassification unwinds first.
        reverse_journal(link.journal, actor_user=actor_user, date=date, document_owner=payment)
    reversal = reverse_journal(
        payment.journal, actor_user=actor_user, date=date,
        document_owner=payment,
    )
    for allocation in allocations:
        invoice = locked_invoices[allocation.vendor_invoice_id]
        # Allocation history remains attached to the reversed payment; only the
        # authoritative invoice settlement totals are rolled back.
        invoice.amount_paid = max(0, invoice.amount_paid - allocation.amount)
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])

    payment.status = DocumentStatus.REVERSED
    payment.save(update_fields=["status", "updated_at"])
    return reversal
