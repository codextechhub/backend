"""AR adjustment services - credit/debit notes, refunds and bad-debt write-offs.

The companion to :mod:`vs_finance.receivables`: where that layer *bills and collects*,
this one *gives back, charges more, refunds and writes off*. Like the rest of the AR
core it is domain-neutral - it speaks only generic customers, invoices and accounts.

The postings raised here:

* **Credit note** (``Dr revenue/returns + Dr output tax, Cr AR``) - reduce a customer's
  receivable for a return, allowance or over-bill; optionally *applied* to invoices
  (a non-cash settlement that bumps :attr:`Invoice.amount_credited`).
* **Debit note** (``Dr AR, Cr revenue + Cr output tax``) - charge a customer more; a
  supplementary invoice, so never applied to reduce another invoice.
* **Refund** (``Dr AR control, Cr bank``) - hand cash back for an over-paid credit
  balance, restoring the receivable.
* **Write-off** (``Dr bad-debt expense, Cr AR control``) - concede an uncollectable
  receivable; clears the invoice's balance via ``amount_credited``.

All amounts are integer kobo; tax uses the same ``ROUND_HALF_UP`` discipline as
:mod:`vs_finance.receivables`.
"""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    CreditNoteKind,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import FinanceError, PostingError
from .money import format_naira
from .posting import post_journal, resolve_period
from .receivables import _build_invoice_plan, compute_line_net, compute_tax


# --------------------------------------------------------------------------- #
# Pricing                                                                      #
# --------------------------------------------------------------------------- #

# Recalculate credit/debit note line and header totals.
def price_credit_note(note) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the note totals.

    Idempotent: safe to call repeatedly while the note is still a draft.
    """
    from .models import CreditNoteLine

    for line in note.lines.all():  # Reprice every note line.
        net = compute_line_net(line.quantity, line.unit_price)  # Compute line net in kobo.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use output tax rate when present.
        tax = compute_tax(net, rate)  # Compute tax in kobo.
        if line.net_amount != net or line.tax_amount != tax:  # Avoid unnecessary writes.
            CreditNoteLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    note.recompute_totals(save=True)  # Roll line totals up to note header.


# --------------------------------------------------------------------------- #
# Credit / debit note posting                                                  #
# --------------------------------------------------------------------------- #

# Public wrapper for credit/debit note posting.
def post_credit_note(note, *, actor_user=None, auto_allocate=False, allocations=None):
    """Price, validate and post a :class:`CreditNote`, raising its AR journal.

    For a CREDIT note, ``allocations`` (a list of ``(invoice, amount_kobo)``) - or
    ``auto_allocate`` - applies the credit to open invoices oldest-first. DEBIT notes
    increase the receivable and are never allocated.
    """
    try:  # Atomic worker performs posting and optional allocation.
        result = _post_credit_note_atomic(  # Post the note.
            note, actor_user=actor_user,  # Acting user.
            auto_allocate=auto_allocate, allocations=allocations,  # Allocation mode.
        )
    except FinanceError as exc:  # Failed notes should be auditable.
        action = (  # Choose audit action based on note direction.
            FinanceAuditAction.DEBIT_NOTE_POSTED if note.kind == CreditNoteKind.DEBIT  # Debit-note failure action.
            else FinanceAuditAction.CREDIT_NOTE_POSTED  # Credit-note failure action.
        )
        record_rejection(  # Record durable rejection.
            entity=note.entity, action=action,  # Entity and selected action.
            exc=exc, actor_user=actor_user, target=note,  # Error, actor, and target context.
        )
        raise
    # Best-effort customer notice after the money posting succeeds. Delivery failure
    # is recorded by vs_notifications and must never roll back the ledger.
    from .notifications import notify_credit_note_issued
    notify_credit_note_issued(result, actor_user=actor_user)
    return result


@transaction.atomic
# Transactional note posting.
def _post_credit_note_atomic(note, *, actor_user=None, auto_allocate=False, allocations=None):
    """Post a draft credit/debit note: raise its AR journal (and, for a credit, settle
    invoices) in one transaction.

    One ``note.kind`` drives two mirror-image postings, so the body forks on
    ``is_debit``. Everything runs atomically - subledger writes (allocation rows +
    invoice ``amount_credited``) and the GL journal commit together or not at all.

    Steps:
      1. **Guard.** Only a DRAFT note posts, and the customer must have an AR control
         account (both sides of the entry touch it).
      2. **Price.** ``price_credit_note`` recomputes each line's net/tax and rolls up
         the totals; the note total must then be positive.
      3. **Group the lines.** Revenue is bucketed by ``(revenue_account, cost_center)``
         so the cost-centre split survives into the GL; output tax is aggregated by
         its collected (output-tax) account. This keeps the journal to one line per
         distinct account instead of one per note line.
      4. **Post the journal - direction depends on kind:**
         * **DEBIT note** (charge more, a supplementary invoice): ``Dr AR`` (gross),
           ``Cr revenue`` + ``Cr output tax``. Never allocated → ``applied = 0``.
         * **CREDIT note** (give value back): ``Dr revenue/returns`` +
           ``Dr output-tax reversal``, then split the credit like a payment -
           ``_build_invoice_plan`` picks which invoices to settle (explicit
           ``allocations`` or ``auto_allocate`` oldest-first), and
           ``_apply_creditnote_subledger`` writes the CreditNoteAllocation rows and
           bumps each invoice's ``amount_credited`` (no GL - this function owns the
           journal), returning ``applied``. The applied portion credits AR
           (``Cr AR``); any ``excess`` is booked to the customer-credit liability
           (``Cr 2140``) so AR never carries a credit balance. That stored credit is
           later drained by ``allocate_credit_note``.
      5. **Balance & post.** ``post_journal`` validates the entry balances and marks
         it posted.
      6. **Finalise.** Link the journal, flip status to POSTED, store
         ``allocated_amount`` (credit notes only), and write a
         CREDIT_NOTE_POSTED / DEBIT_NOTE_POSTED audit record.

    Returns the updated ``note``. Raises ``PostingError`` on any guard failure;
    ``post_credit_note`` wraps this to record a rejection on ``FinanceError``.
    """
    from .models import JournalEntry, JournalLine

    if note.status != DocumentStatus.DRAFT:  # Only draft notes can post.
        raise PostingError(
            f"Credit note {note.document_number or note.pk} is '{note.status}', "
            f"only a draft note can be posted.",
        )

    customer = note.customer  # Customer drives AR control account.
    ar_account = customer.receivable_account  # Customer AR account.
    if ar_account is None:  # AR side cannot post without a control account.
        raise PostingError(
            f"Customer {customer.code} has no receivable (AR control) account set.",
        )

    price_credit_note(note)  # Ensure note totals are current before posting.
    if note.total <= 0:  # Note must move a positive amount.
        raise PostingError("A credit/debit note must have a positive total to post.")

    is_debit = note.kind == CreditNoteKind.DEBIT  # Debit notes increase receivables; credit notes reduce them.
    period = resolve_period(note.entity, note.note_date)  # Resolve note accounting period.
    label = "Debit note" if is_debit else "Credit note"  # Human label for journal/audit text.
    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch,  # Scope entity and optional branch.
        date=note.note_date, period=period,  # Note date and period.
        source=JournalSource.SALES, currency=note.currency,  # Sales-side AR adjustment.
        narration=note.reason or f"{label} {note.document_number or ''}".strip(),  # Reason/narration.
        reference=note.reference, created_by=actor_user,  # External reference and actor.
    )

    # Group revenue + tax by account so the journal stays tidy.
    # Revenue grouped by (account, cost centre) so the cost-centre split survives into
    # the GL; tax stays aggregated by account (it's a liability, not P&L analytics).
    revenue_by_key: dict[tuple[int, int | None], int] = defaultdict(int)  # Net amount grouped by revenue account/cost center.
    revenue_objs: dict[tuple[int, int | None], tuple] = {}  # Revenue account/cost center objects.
    tax_by_account: dict[int, int] = defaultdict(int)  # Output tax amount grouped by tax account.
    tax_objs: dict[int, object] = {}  # Tax account objects.
    for line in note.lines.select_related(
        "revenue_account", "tax_code__collected_account", "cost_center",  # Revenue, tax, and analytics relations.
    ):
        key = (line.revenue_account_id, line.cost_center_id)  # Revenue grouping key.
        revenue_by_key[key] += line.net_amount  # Accumulate net amount.
        revenue_objs[key] = (line.revenue_account, line.cost_center)  # Store objects for journal lines.
        if line.tax_amount:  # Tax-bearing lines require output tax account.
            tax_acc = line.tax_code.collected_account if line.tax_code_id else None  # Resolve output tax account.
            if tax_acc is None:  # Cannot post tax without a collected account.
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no collected (output) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount  # Accumulate output tax amount.
            tax_objs[tax_acc.id] = tax_acc  # Store tax account object.

    line_no = 0  # Journal line counter.
    if is_debit:  # Debit note charges the customer more.
        # Dr AR (gross), Cr revenue + Cr output tax - a supplementary charge.  # Mirror of invoice posting.
        line_no += 1  # First line is AR debit.
        JournalLine.objects.create(
            entry=entry, account=ar_account, debit=note.total, credit=0,  # Dr receivables.
            description=f"AR: {customer.code}", line_no=line_no,  # Label and order.
        )
        for (acc_id, cc_id), amount in revenue_by_key.items():  # Emit grouped revenue credits.
            if amount == 0:  # Skip empty groups.
                continue
            line_no += 1  # Advance line number.
            revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]  # Retrieve posting objects.
            JournalLine.objects.create(
                entry=entry, account=revenue_account, debit=0, credit=amount,  # Cr revenue.
                description="Revenue", cost_center=cost_center, line_no=line_no,  # Preserve cost center.
            )
        for acc_id, amount in tax_by_account.items():  # Emit grouped output tax credits.
            line_no += 1  # Advance line number.
            JournalLine.objects.create(
                entry=entry, account=tax_objs[acc_id], debit=0, credit=amount,  # Cr output tax.
                description="Output tax", line_no=line_no,  # Label and order.
            )
        applied = 0  # Debit notes are never allocated to invoices.
    else:  # Credit note gives value back.
        # Dr revenue/returns + Dr output tax - give value back. The credit settles
        # invoices (Cr AR) for the applied portion; the unapplied remainder becomes a
        # customer-credit liability (Cr 2140) so AR never carries a credit balance.  # Keep AR non-negative.
        for (acc_id, cc_id), amount in revenue_by_key.items():  # Emit grouped revenue/return debits.
            if amount == 0:  # Skip empty groups.
                continue
            line_no += 1  # Advance line number.
            revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]  # Retrieve posting objects.
            JournalLine.objects.create(
                entry=entry, account=revenue_account, debit=amount, credit=0,  # Dr revenue/returns.
                description="Revenue / returns", cost_center=cost_center, line_no=line_no,  # Preserve cost center.
            )
        for acc_id, amount in tax_by_account.items():  # Emit grouped tax reversals.
            line_no += 1  # Advance line number.
            JournalLine.objects.create(
                entry=entry, account=tax_objs[acc_id], debit=amount, credit=0,  # Dr output tax.
                description="Output tax reversal", line_no=line_no,  # Label and order.
            )
        plan = (_build_invoice_plan(  # Build allocation plan, bounded by the note's own date.
            customer, allocations, as_of=note.note_date,
            settlement=f"Credit note {note.document_number or note.pk}",
        ) if (allocations is not None or auto_allocate) else [])
        applied, _created, _latest = _apply_creditnote_subledger(note, plan, remaining=note.total)  # Apply credit to invoices.
        excess = note.total - applied  # Unapplied credit becomes customer-credit liability.
        if applied > 0:  # Applied credit reduces AR.
            line_no += 1  # Advance line number.
            JournalLine.objects.create(
                entry=entry, account=ar_account, debit=0, credit=applied,  # Cr receivables.
                description=f"AR: {customer.code}", line_no=line_no,  # Label and order.
            )
        if excess > 0:  # Unapplied credit creates a liability.
            line_no += 1  # Advance line number.
            JournalLine.objects.create(
                entry=entry, account=resolve_mapped_account(note.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),  # Resolve liability account.
                debit=0, credit=excess, description=f"Customer credit: {customer.code}", line_no=line_no,  # Cr customer credit.
            )

    post_journal(entry, actor_user=actor_user)  # Validate and post note journal.

    note.journal = entry  # Link note to journal.
    note.status = DocumentStatus.POSTED  # Mark note posted.
    if not is_debit:  # Credit notes track allocated amount.
        note.allocated_amount = applied  # Store initially applied credit.
        note.save(update_fields=["journal", "status", "allocated_amount", "updated_at"])
    else:  # Debit notes have no allocation state.
        note.save(update_fields=["journal", "status", "updated_at"])

    record(  # Audit successful note posting.
        entity=note.entity,  # Entity context.
        action=(FinanceAuditAction.DEBIT_NOTE_POSTED if is_debit  # Debit note audit action.
                else FinanceAuditAction.CREDIT_NOTE_POSTED),  # Credit note audit action.
        actor_user=actor_user, target=note,  # Actor and target context.
        message=f"Posted {label.lower()} for {customer.code} ({note.total} kobo).",  # Summary.
        journal_id=entry.pk, total=note.total, note_kind=note.kind,  # Structured metadata.
    )
    return note  # Return posted note.


# Apply credit-note value to invoice subledger.
def _apply_creditnote_subledger(note, plan, *, remaining):
    """Create/extend CreditNoteAllocation rows + bump invoice ``amount_credited`` for
    the plan, capped at each invoice balance and ``remaining``. GL-agnostic - the
    caller posts the journal.

    Returns ``(applied_total, created_rows, latest_invoice_date)``; the caller needs
    that last date to book its reclassification on or after the invoice it clears."""
    from .models import CreditNoteAllocation

    applied, created = 0, []  # Track total applied and touched allocation rows.
    latest = None  # Newest invoice date this run actually settled.
    for invoice, requested in plan:  # Walk requested allocation plan.
        if remaining <= 0:  # Stop when note value is exhausted.
            break  # Exit the current loop.
        apply_amount = min(int(requested), invoice.balance_due, remaining)  # Cap by request, balance, and remaining credit.
        if apply_amount <= 0:  # Skip zero/negative allocations.
            continue
        alloc, _was = CreditNoteAllocation.objects.get_or_create(
            note=note, invoice=invoice, defaults={"amount": 0},  # Unique note/invoice allocation.
        )
        alloc.amount += apply_amount  # Increase allocated amount.
        alloc.save(update_fields=["amount", "updated_at"])

        invoice.amount_credited += apply_amount  # Increase non-cash settlement on invoice.
        invoice.refresh_payment_status(save=False)  # Recompute paid/partial/unpaid state.
        invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
        # Keep any installment plan on this invoice in step with the new settlement.  # Plans mirror invoice settlement.
        from .installments import refresh_plans_for_invoice
        refresh_plans_for_invoice(invoice)  # Refresh linked plan progress.

        remaining -= apply_amount  # Reduce available note value.
        applied += apply_amount  # Increase applied total.
        created.append(alloc)  # Track allocation row.
        if latest is None or invoice.invoice_date > latest:  # Track newest settled invoice.
            latest = invoice.invoice_date
    return applied, created, latest  # Applied total, allocation rows, newest settled date.


@transaction.atomic
# Allocate stored credit-note liability to invoices.
def allocate_credit_note(note, *, allocations=None, actor_user=None):
    """Apply a posted CREDIT note's **stored customer credit** to invoices.

    Any unapplied portion of the note sits in the customer-credit liability (2140);
    applying it reclassifies it back to AR (``Dr customer-credit · Cr AR``) and
    settles the invoices. ``allocations`` is an optional ``[(invoice, amount)]`` plan;
    without it, open invoices are settled oldest-first.

    An older note may be applied to a newer invoice - that is legitimate - but the
    reclassification is dated at the later of the two, never before the receivable it
    clears exists.
    """
    from .chronology import effective_allocation_date
    from .models import Customer, CustomerCreditAllocationJournal, JournalEntry, JournalLine

    note = type(note).objects.select_for_update().get(pk=note.pk)
    customer = Customer.objects.select_for_update().get(pk=note.customer_id)

    if note.kind == CreditNoteKind.DEBIT:  # Debit notes cannot reduce invoices.
        raise PostingError("A debit note increases the receivable; it cannot be allocated.")
    if note.status != DocumentStatus.POSTED:  # Only posted credit notes have stored credit to apply.
        raise PostingError("Only a posted credit note can be allocated.")

    # Credit *remaining*: value already refunded out has left 2140 and cannot be
    # reclassified to AR as well.
    remaining = note.credit_remaining  # Customer-credit liability still available.
    if remaining <= 0:  # Nothing left to allocate.
        return []

    plan = _build_invoice_plan(note.customer, allocations)  # Build explicit or oldest-first invoice plan.
    applied, created, latest = _apply_creditnote_subledger(note, plan, remaining=remaining)  # Apply credit to invoices.
    if applied <= 0:  # No invoice received value.
        return []

    effective = effective_allocation_date(note.note_date, [latest])  # Later of note and settled invoices.
    period = resolve_period(note.entity, effective)  # Resolve allocation period.
    entry = JournalEntry.objects.create(
        entity=note.entity, branch=note.branch,  # Scope entity and optional branch.
        date=effective, period=period,  # Effective allocation date and its period.
        source=JournalSource.SALES, currency=note.currency,  # Sales-side reclassification.
        narration=f"Apply customer credit: {customer.code}",  # Journal narration.
        reference=note.reference, created_by=actor_user,  # Reference and actor.
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_mapped_account(note.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),  # Resolve liability account.
        debit=applied, credit=0, description=f"Customer credit applied: {customer.code}", line_no=1,  # Dr customer credit.
    )
    JournalLine.objects.create(
        entry=entry, account=customer.receivable_account, debit=0, credit=applied,  # Cr receivables.
        description=f"AR: {customer.code}", line_no=2,  # Label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post allocation journal.

    CustomerCreditAllocationJournal.objects.create(
        note=note, journal=entry, amount=applied,
    )

    note.allocated_amount += applied  # Increase allocated credit-note amount.
    note.save(update_fields=["allocated_amount", "updated_at"])

    record(  # Audit credit-note allocation.
        entity=note.entity, action=FinanceAuditAction.CREDIT_NOTE_ALLOCATED,  # Audit action.
        actor_user=actor_user, target=note,  # Actor and target context.
        message=f"Applied {applied} kobo customer credit across {len(created)} invoice(s).",  # Summary.
        journal_id=entry.pk, allocated=note.allocated_amount,  # Structured metadata.
        unallocated=note.credit_remaining, effective_date=str(effective),  # Credit left and effective date.
    )
    return created  # Return allocation rows touched.


# --------------------------------------------------------------------------- #
# Customer refund                                                              #
# --------------------------------------------------------------------------- #


# Find the earliest date a customer's credit could cover an amount.
def _earliest_credit_date(customer, amount, *, exclude_refund_id=None):
    """The first accounting date on which ``amount`` of credit exists, or ``None``.

    Purely for the error message on a backdated refund: telling the user *which* date
    would work turns a flat rejection into a one-click correction. Walks the customer's
    credit lots oldest-first and returns the date of the lot that tips the running
    total over the requested amount.
    """
    from .chronology import credit_lots

    lots = credit_lots(customer.entity, [customer.pk]).get(customer.pk, [])
    running = 0  # Credit accumulated as the timeline advances.
    for lot in lots:  # Lots already arrive oldest-first.
        running += lot.remaining  # Add this parcel to the running credit.
        if running >= amount:  # This lot's date is the first that could fund the payout.
            return lot.date
    return None  # Even the full history cannot cover it; the shortfall is not about dating.


# Attribute a refund to the credit lots it drains.
def _attribute_refund_to_lots(refund, customer, *, as_of):
    """Draw ``refund.amount`` from the customer's credit lots FIFO and record it.

    Writes one :class:`~vs_finance.models.RefundAllocation` per lot touched and bumps
    that lot's ``refunded_amount``, so a receipt stops advertising cash that has been
    handed back. Without this the GL and the sub-ledger disagree: 2140 drains, but the
    originating receipt still reports its cash as unapplied and available - which is
    exactly how the same money could be allocated or refunded twice.

    Returns the created allocation rows. Raises :class:`PostingError` if the lots
    cannot cover the amount, which means an availability guard upstream was wrong.
    """
    from .chronology import credit_lots, plan_credit_draw
    from .models import CreditNote, Payment, RefundAllocation

    lots = credit_lots(refund.entity, [customer.pk], as_of=as_of).get(customer.pk, [])
    plan = plan_credit_draw(lots, refund.amount)  # Choose the parcels to drain, oldest first.
    drawn = sum(taken for _lot, taken in plan)  # Total the plan actually covers.
    if drawn < refund.amount:  # Availability guard and lot arithmetic must agree.
        raise PostingError(
            f"Only {format_naira(drawn)} of identifiable customer credit could be "
            f"matched to this {format_naira(refund.amount)} refund as at "
            f"{as_of}. Refresh the customer's credit and try again.",
        )

    rows = []  # Allocation rows written for this refund.
    for lot, taken in plan:  # Record each draw and drain its source document.
        if lot.kind == "RECEIPT":  # Cash receipt lot.
            source = Payment.objects.select_for_update().get(pk=lot.document_id)
            rows.append(RefundAllocation.objects.create(
                refund=refund, payment=source, amount=taken))
        else:  # CREDIT-note lot.
            source = CreditNote.objects.select_for_update().get(pk=lot.document_id)
            rows.append(RefundAllocation.objects.create(
                refund=refund, note=source, amount=taken))
        source.refunded_amount += taken  # Keep the lot's own read model in step.
        source.save(update_fields=["refunded_amount", "updated_at"])
    return rows


# Public wrapper for customer refund posting.
def post_refund(refund, *, actor_user=None):
    """Post a customer :class:`Refund` (``Dr customer-credit (2140), Cr bank``).

    A refund pays out a customer's credit balance - so it draws down the
    customer-credit liability, not AR. Capped at the customer's available credit.
    Records a durable rejection audit on any :class:`FinanceError`, then re-raises.
    """
    try:  # Atomic worker performs refund posting.
        return _post_refund_atomic(refund, actor_user=actor_user)  # Post refund.
    except FinanceError as exc:  # Failed refunds should be auditable.
        record_rejection(  # Record durable rejection.
            entity=refund.entity, action=FinanceAuditAction.REFUND_POSTED,  # Audit action.
            exc=exc, actor_user=actor_user, target=refund,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Transactional customer refund implementation.
def _post_refund_atomic(refund, *, actor_user=None):
    """Post a draft refund: raise its bank journal and mark it POSTED.
    Steps:
      1. **Guard.** Only a DRAFT refund posts, and the amount must be positive and not exceed the customer's available credit.
      2. **Post the journal** (``Dr customer-credit (2140), Cr bank``) to pay out the refund.
      3. **Finalise.** Link the journal, flip status to POSTED, and write a REFUND_POSTED audit record.
        Returns the updated ``refund``. Raises ``PostingError`` on any guard failure;
        ``post_refund`` wraps this to record a rejection on ``FinanceError``.
    """
    from .models import Customer, JournalEntry, JournalLine

    # Serialize every payout for this customer before reading the derived credit
    # position. Without this lock, two different refund rows could both observe the
    # same balance and pay it out concurrently.
    refund = type(refund).objects.select_for_update().get(pk=refund.pk)
    customer = Customer.objects.select_for_update().get(pk=refund.customer_id)

    if refund.status != DocumentStatus.DRAFT:  # Only draft refunds can post.
        raise PostingError(
            f"Refund {refund.document_number or refund.pk} is '{refund.status}', "
            f"only a draft refund can be posted.",
        )
    if refund.amount <= 0:  # Refund must pay a positive amount.
        raise PostingError("A refund must have a positive amount to post.")

    # Availability is measured **on the refund's own accounting date**, not today.
    # Credit that only arrives later has not happened yet as far as this payout is
    # concerned, and paying it out would drive the 2140 liability negative for the gap.
    from .receivables import customer_refund_available_balance
    available = customer_refund_available_balance(
        customer, exclude_refund_id=refund.pk,
        as_of=refund.refund_date)  # Unreserved refundable credit as at the refund date.
    if refund.amount > available:  # Refund cannot exceed stored customer credit.
        later = customer_refund_available_balance(
            customer, exclude_refund_id=refund.pk)  # Same figure with no date cutoff.
        hint = ""
        if later > available:  # The shortfall is purely a dating problem - say so.
            first = _earliest_credit_date(customer, refund.amount, exclude_refund_id=refund.pk)
            hint = (
                f" {format_naira(later)} is available today, but not as at "
                f"{refund.refund_date}"
                + (f" - the credit exists from {first}." if first else ".")
            )
        raise PostingError(
            f"Refund of {format_naira(refund.amount)} exceeds {customer.code}'s credit "
            f"available on {refund.refund_date} ({format_naira(available)}).{hint}",
        )

    deposit = refund.deposit_account or (  # Resolve bank/deposit account to credit.
        refund.bank_account.gl_account if refund.bank_account_id else None  # Fallback from selected bank account.
    )
    if deposit is None:  # Refund needs a payment source account.
        raise PostingError("Refund has no bank/deposit account to pay from.")

    period = resolve_period(refund.entity, refund.refund_date)  # Resolve refund period.
    entry = JournalEntry.objects.create(
        entity=refund.entity, branch=refund.branch,  # Scope entity and optional branch.
        date=refund.refund_date, period=period,  # Refund date and period.
        source=JournalSource.BANK, currency=refund.currency,  # Bank-source cash payment.
        narration=refund.narration or f"Refund {refund.document_number or ''}".strip(),  # Narration.
        reference=refund.reference, created_by=actor_user,  # Reference and actor.
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_mapped_account(refund.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),  # Resolve liability.
        debit=refund.amount, credit=0, description=f"Refund: {customer.code}", line_no=1,  # Dr customer credit.
    )
    JournalLine.objects.create(
        entry=entry, account=deposit, debit=0, credit=refund.amount,  # Cr cash/bank.
        description=f"Refund paid: {customer.code}", line_no=2,  # Label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post refund journal.

    # Say which credit was handed back, not just that some was. Same transaction as
    # the journal, so the sub-ledger can never drift from the 2140 movement.
    rows = _attribute_refund_to_lots(refund, customer, as_of=refund.refund_date)

    refund.journal = entry  # Link refund to journal.
    refund.deposit_account = deposit  # Persist account used.
    refund.status = DocumentStatus.POSTED  # Mark refund posted.
    refund.save(update_fields=["journal", "deposit_account", "status", "updated_at"])

    record(  # Audit successful refund.
        entity=refund.entity, action=FinanceAuditAction.REFUND_POSTED,  # Audit action.
        actor_user=actor_user, target=refund,  # Actor and target context.
        message=f"Refunded {refund.amount} kobo to {customer.code}.",  # Summary.
        journal_id=entry.pk, amount=refund.amount,  # Structured metadata.
        drawn_from=[  # Which credit parcels funded the payout.
            {
                "source": (row.payment or row.note).document_number,
                "amount": row.amount,
            }
            for row in rows
        ],
    )
    return refund  # Return posted refund.


# --------------------------------------------------------------------------- #
# Bad-debt write-off                                                           #
# --------------------------------------------------------------------------- #

# Handle the write off invoice workflow.
def write_off_invoice(invoice, *, amount=None, write_off_account=None,
                      write_off_date=None, narration="", actor_user=None):  # Public wrapper for invoice write-off.
    """Write off an uncollectable invoice balance as bad debt.

    Posts ``Dr bad-debt expense, Cr AR control`` for ``amount`` (defaulting to the
    full outstanding balance) and clears that much of the invoice via
    ``amount_credited``. ``write_off_account`` defaults to the entity's bad-debt /
    general expense account (CoA ``5300``).
    """
    try:  # Atomic worker performs bad-debt posting.
        return _write_off_invoice_atomic(  # Write off invoice balance.
            invoice, amount=amount, write_off_account=write_off_account,  # Amount and optional account.
            write_off_date=write_off_date, narration=narration, actor_user=actor_user,  # Date, narration, actor.
        )
    except FinanceError as exc:  # Failed write-offs should be auditable.
        record_rejection(  # Record durable rejection.
            entity=invoice.entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,  # Audit action.
            exc=exc, actor_user=actor_user, target=invoice,  # Error, actor, and target context.
        )
        raise


@transaction.atomic
# Support the write off invoice atomic workflow.
def _write_off_invoice_atomic(invoice, *, amount=None, write_off_account=None,
                              write_off_date=None, narration="", actor_user=None):  # Transactional bad-debt write-off.
    from .models import JournalEntry, JournalLine

    if invoice.status != DocumentStatus.POSTED:  # Only posted invoices have AR balances.
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
            f"only a posted invoice can be written off.",
        )

    balance = invoice.balance_due  # Current outstanding invoice balance.
    if balance <= 0:  # Fully settled invoices cannot be written off.
        raise PostingError("Invoice has no outstanding balance to write off.")
    amount = balance if amount in (None, "") else int(amount)  # Default to full outstanding balance.
    if amount <= 0:  # Write-off must clear a positive amount.
        raise PostingError("Write-off amount must be positive.")
    if amount > balance:  # Cannot write off more than outstanding.
        raise PostingError(
            f"Write-off amount ({amount} kobo) exceeds the outstanding balance "
            f"({balance} kobo).",
        )

    customer = invoice.customer  # Customer controls AR account.
    ar_account = customer.receivable_account  # AR control account.
    if ar_account is None:  # Credit side needs AR account.
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")

    expense = write_off_account or resolve_mapped_account(  # Use explicit write-off account or default.
        invoice.entity, AccountMappingKey.BAD_DEBT_EXPENSE, label="bad-debt expense",  # Resolve bad-debt expense account.
    )
    when = write_off_date or invoice.invoice_date  # Default write-off date to invoice date.
    # A debt cannot be conceded before it is owed: writing off on a date earlier than
    # the invoice credits AR before the invoice ever debited it.
    from .chronology import ensure_on_or_after
    ensure_on_or_after(
        subject=f"Write-off of {invoice.document_number or invoice.pk}", subject_date=when,
        source=f"invoice {invoice.document_number or invoice.pk}",
        source_date=invoice.invoice_date,
        remedy=f"Date the write-off {invoice.invoice_date} or later.",
    )
    period = resolve_period(invoice.entity, when)  # Resolve write-off period.
    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,  # Scope entity and optional branch.
        date=when, period=period, source=JournalSource.SALES,  # Sales-side AR adjustment.
        narration=narration or f"Write-off {invoice.document_number or ''}".strip(),  # Narration.
        created_by=actor_user,  # Posting actor.
    )
    JournalLine.objects.create(
        entry=entry, account=expense, debit=amount, credit=0,  # Dr bad debt.
        description=f"Bad debt: {customer.code}", line_no=1,  # Label and order.
    )
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=0, credit=amount,  # Cr receivables.
        description=f"AR write-off: {customer.code}", line_no=2,  # Label and order.
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post write-off journal.

    invoice.amount_credited += amount  # Increase non-cash settlement.
    invoice.refresh_payment_status(save=False)  # Recompute invoice payment status.
    invoice.save(update_fields=["amount_credited", "payment_status", "updated_at"])
    # A write-off reduces the outstanding balance, so any installment plan tracks it too.  # Keep plans in sync.
    from .installments import refresh_plans_for_invoice
    refresh_plans_for_invoice(invoice, actor_user=actor_user)  # Refresh linked payment plans.

    record(  # Audit successful write-off.
        entity=invoice.entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,  # Audit action.
        actor_user=actor_user, target=invoice,  # Actor and invoice target.
        message=f"Wrote off {amount} kobo of invoice {invoice.document_number} "  # Human-readable summary.
                f"for {customer.code}.",  # Customer context.
        journal_id=entry.pk, amount=amount, balance_after=invoice.balance_due,  # Journal and balance metadata.
        narration=narration or "", customer_code=customer.code, customer_name=customer.name,  # Extra audit context.
    )
    return entry  # Return posted write-off journal.


# Post an approved/draft write-off request.
def post_write_off_request(wor, *, actor_user=None):
    """Post a :class:`~vs_finance.models.WriteOffRequest`, running the write-off.

    Thin adapter over the unchanged :func:`write_off_invoice` service: it validates
    the request is in a postable state, delegates the actual GL work to
    ``write_off_invoice`` (which posts the bad-debt journal, clears the invoice via
    ``amount_credited`` and writes the audit row), then links the returned journal and
    flips the request POSTED.

    ``status`` must be DRAFT (the ungated direct-post path) or APPROVED (the approval
    path, after the workflow base handler flips it) - both are valid entry states.
    Any :class:`~vs_finance.exceptions.FinanceError` raised by ``write_off_invoice``
    propagates unchanged (it records its own durable rejection audit); on the approval
    path that rollback leaves the request non-POSTED for a retry. Returns the request.
    """
    if wor.status not in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):  # Request must be direct-postable or workflow-approved.
        raise PostingError(
            f"Write-off {wor.document_number or wor.pk} is '{wor.status}'; "
            f"only a draft or approved write-off request can be posted.",
        )

    entry = write_off_invoice(  # Delegate GL/accounting work to invoice write-off service.
        wor.invoice,  # Target invoice.
        amount=wor.amount or None,  # Optional request amount.
        write_off_account=wor.write_off_account,  # Optional write-off account.
        write_off_date=wor.write_off_date,  # Optional write-off date.
        narration=wor.narration,  # Request narration.
        actor_user=actor_user,  # Posting actor.
    )

    wor.journal = entry  # Link request to write-off journal.
    wor.status = DocumentStatus.POSTED  # Mark request posted.
    wor.save(update_fields=["journal", "status", "updated_at"])
    return wor  # Return posted request.
