"""Accounts-Receivable services — the revenue cycle.

Domain-neutral on purpose: these functions move generic invoices and payments into
the General Ledger and never mention students, parents or fees. A billing *source*
(a school fee run, a subscription engine) is just whatever creates the
:class:`~vs_finance.models.Invoice` rows; from here down it's pure double-entry.

The two postings this layer raises:

* **Invoice** → ``Dr receivable control, Cr revenue (per line), Cr output tax``.
* **Payment** → ``Dr bank/cash, Cr receivable control`` — then the cash is *allocated*
  across invoices (a sub-ledger act with no further GL effect).

All amounts are integer kobo; tax is computed from basis points with the same
``ROUND_HALF_UP`` discipline as :mod:`vs_finance.money`.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from .audit import record, record_rejection
from .constants import (
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import FinanceError, PostingError
from .posting import post_journal, resolve_period


# --------------------------------------------------------------------------- #
# Money helpers (integer kobo)                                                 #
# --------------------------------------------------------------------------- #

def compute_line_net(quantity, unit_price_kobo: int) -> int:
    """``quantity × unit_price`` in kobo, rounded half-up to a whole kobo."""
    amount = Decimal(quantity) * Decimal(int(unit_price_kobo))
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_tax(net_kobo: int, rate_bps: int) -> int:
    """Tax on ``net_kobo`` at ``rate_bps`` basis points (750 = 7.5%), half-up to kobo.

    Integer-exact: a tax line is never carried as a float.
    """
    if not rate_bps:
        return 0
    amount = Decimal(int(net_kobo)) * Decimal(int(rate_bps)) / Decimal(10000)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price_invoice(invoice) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the invoice totals.

    Idempotent: safe to call repeatedly while an invoice is still a draft.
    """
    from .models import InvoiceLine

    for line in invoice.lines.all():
        net = compute_line_net(line.quantity, line.unit_price)
        rate = line.tax_code.rate_bps if line.tax_code_id else 0
        tax = compute_tax(net, rate)
        if line.net_amount != net or line.tax_amount != tax:
            InvoiceLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    invoice.recompute_totals(save=True)


# --------------------------------------------------------------------------- #
# Invoice posting                                                             #
# --------------------------------------------------------------------------- #

def post_invoice(invoice, *, actor_user=None):
    """Price, validate and post an :class:`Invoice`, raising its AR journal.

    Wrapper that records a durable rejection audit on any :class:`FinanceError`, then
    re-raises — mirroring the journal posting contract.
    """
    try:
        return _post_invoice_atomic(invoice, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=invoice.entity,
            action=FinanceAuditAction.INVOICE_POSTED,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise


@transaction.atomic
def _post_invoice_atomic(invoice, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if invoice.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}', "
            f"only a draft invoice can be posted.",
        )

    customer = invoice.customer
    ar_account = customer.receivable_account
    if ar_account is None:
        raise PostingError(
            f"Customer {customer.code} has no receivable (AR control) account set.",
        )

    price_invoice(invoice)
    if invoice.total <= 0:
        raise PostingError("An invoice must have a positive total to post.")

    period = resolve_period(invoice.entity, invoice.invoice_date)

    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,
        date=invoice.invoice_date, period=period,
        source=JournalSource.SALES, currency=invoice.currency,
        narration=invoice.narration or f"Invoice {invoice.document_number or ''}".strip(),
        reference=invoice.reference, created_by=actor_user,
    )

    line_no = 0
    # Dr the receivable control for the gross total.
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=invoice.total, credit=0,
        description=f"AR: {customer.code}", line_no=line_no,
    )
    # Cr revenue, grouped by account so the journal is tidy.
    revenue_by_account: dict[int, int] = defaultdict(int)
    revenue_objs: dict[int, object] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in invoice.lines.select_related("revenue_account", "tax_code__collected_account"):
        revenue_by_account[line.revenue_account_id] += line.net_amount
        revenue_objs[line.revenue_account_id] = line.revenue_account
        if line.tax_amount:
            tax_acc = line.tax_code.collected_account if line.tax_code_id else None
            if tax_acc is None:
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no collected (output) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    for acc_id, amount in revenue_by_account.items():
        if amount == 0:
            continue
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=revenue_objs[acc_id], debit=0, credit=amount,
            description="Revenue", line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=0, credit=amount,
            description="Output tax", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)

    invoice.journal = entry
    invoice.status = DocumentStatus.POSTED
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["journal", "status", "payment_status", "updated_at"])

    record(
        entity=invoice.entity, action=FinanceAuditAction.INVOICE_POSTED,
        actor_user=actor_user, target=invoice,
        message=f"Posted invoice for {customer.code} ({invoice.total} kobo).",
        journal_id=entry.pk, total=invoice.total, tax=invoice.tax_total,
    )
    return invoice


# --------------------------------------------------------------------------- #
# Payment posting + allocation                                                #
# --------------------------------------------------------------------------- #

def post_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    """Post a customer :class:`Payment` (Dr bank, Cr AR) and allocate it to invoices.

    ``allocations`` (a list of ``(invoice, amount_kobo)``) applies an explicit split;
    otherwise ``auto_allocate`` settles the customer's oldest open invoices first.
    """
    try:
        return _post_payment_atomic(
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations,
        )
    except FinanceError as exc:
        record_rejection(
            entity=payment.entity,
            action=FinanceAuditAction.PAYMENT_POSTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise


@transaction.atomic
def _post_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None):
    from .models import JournalEntry, JournalLine

    if payment.status != DocumentStatus.DRAFT:
        raise PostingError(
            f"Payment {payment.document_number or payment.pk} is '{payment.status}', "
            f"only a draft payment can be posted.",
        )
    if payment.amount <= 0:
        raise PostingError("A payment must have a positive amount to post.")

    customer = payment.customer
    ar_account = customer.receivable_account
    if ar_account is None:
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")
    if payment.deposit_account_id is None:
        raise PostingError("Payment has no deposit (bank/cash) account set.")

    period = resolve_period(payment.entity, payment.payment_date)

    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Receipt {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=payment.deposit_account, debit=payment.amount, credit=0,
        description=f"Receipt: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=0, credit=payment.amount,
        description=f"AR: {customer.code}", line_no=2,
    )

    post_journal(entry, actor_user=actor_user)

    payment.journal = entry
    payment.status = DocumentStatus.POSTED
    payment.save(update_fields=["journal", "status", "updated_at"])

    record(
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=f"Posted receipt from {customer.code} ({payment.amount} kobo).",
        journal_id=entry.pk, amount=payment.amount,
    )

    if allocations:
        allocate_payment(payment, allocations=allocations, actor_user=actor_user)
    elif auto_allocate:
        allocate_payment(payment, actor_user=actor_user)
    return payment


@transaction.atomic
def allocate_payment(payment, *, allocations=None, actor_user=None):
    """Apply a posted payment's unallocated cash to invoices.

    ``allocations`` is an optional list of ``(invoice, amount_kobo)``; without it the
    customer's open posted invoices are settled oldest-first (by due date, then
    invoice date). Never allocates past an invoice's balance due or the payment's
    remaining cash. Returns the list of created allocation rows.
    """
    from .models import Invoice, PaymentAllocation

    if payment.status != DocumentStatus.POSTED:
        raise PostingError("Only a posted payment can be allocated.")

    remaining = payment.unallocated_amount
    created = []

    if allocations is None:
        open_invoices = (
            Invoice.objects
            .filter(
                customer=payment.customer, status=DocumentStatus.POSTED,
            )
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
        alloc, was_created = PaymentAllocation.objects.get_or_create(
            payment=payment, invoice=invoice, defaults={"amount": 0},
        )
        alloc.amount += apply_amount
        alloc.save(update_fields=["amount", "updated_at"])

        invoice.amount_paid += apply_amount
        invoice.refresh_payment_status(save=False)
        invoice.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        remaining -= apply_amount
        created.append(alloc)

    payment.allocated_amount = payment.amount - remaining
    payment.save(update_fields=["allocated_amount", "updated_at"])

    if created:
        record(
            entity=payment.entity, action=FinanceAuditAction.PAYMENT_ALLOCATED,
            actor_user=actor_user, target=payment,
            message=f"Allocated {payment.allocated_amount} kobo across {len(created)} invoice(s).",
            allocated=payment.allocated_amount, unallocated=payment.unallocated_amount,
        )
    return created
