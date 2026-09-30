"""Opening balances: what customers owed on the day the books began.

A school moving onto these books arrives with arrears, some of them years old. Each
unpaid bill is carried in as its own opening invoice, dated as the original was and
due when the original fell due, so ageing reads true from the first day: Corona's
2022 arrears age as 2022 arrears, not as a bill raised on go-live day that no
provision would ever touch. The supplier side does the same with opening bills
(:func:`vs_procurement.payables.import_opening_vendor_invoices`); both credit the
entity's opening-balance equity mapping (retained earnings), because what was owed
on day one is prior-period value, never this year's income.
"""
from __future__ import annotations

from django.db import transaction

from .audit import record, record_rejection
from .constants import (
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    InvoiceSource,
    JournalSource,
)
from .exceptions import FinanceError, PostingError
from .money import format_naira


def books_went_live(entity):
    """The first day these books recorded trading, or ``None`` if they have not yet.

    That is the date of the earliest posted journal that is not itself an opening
    balance: not an ``OPENING`` journal (a direct entry, an opening bill or an
    opening customer invoice) and not the journal of an opening customer invoice
    raised with a customer. Anything owed before that day is an opening balance;
    anything after it is ordinary business and is keyed as an ordinary document.
    """
    from .models import JournalEntry

    return (
        JournalEntry.objects.filter(
            entity=entity, status__in=(DocumentStatus.POSTED, DocumentStatus.REVERSED),
            reverses__isnull=True,
        )
        .exclude(source=JournalSource.OPENING)
        .exclude(ar_invoices__source=InvoiceSource.OPENING)
        .order_by("date").values_list("date", flat=True).first()
    )


def _opening_journal_date(entity, invoice_date):
    """The invoice date when a period covers it, else the first day of the books."""
    from .models import FiscalPeriod
    from .posting import resolve_period

    if resolve_period(entity, invoice_date) is not None:
        return invoice_date
    first = (
        FiscalPeriod.objects.filter(entity=entity, is_closing=False)
        .order_by("start_date").first()
    )
    if first is not None and invoice_date < first.start_date:
        return first.start_date
    return invoice_date


def post_opening_invoice(invoice, *, actor_user=None):
    """Post one opening invoice, recording a durable rejection audit row when refused."""
    try:
        return _post_opening_invoice_atomic(invoice, actor_user=actor_user)
    except FinanceError as exc:
        record_rejection(
            entity=invoice.entity, action=FinanceAuditAction.CUSTOMER_OPENING_POSTED,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise


@transaction.atomic
def _post_opening_invoice_atomic(invoice, *, actor_user=None):
    """Seat one unpaid customer bill from before the books began: ``Dr AR, Cr equity``.

    The invoice keeps its own invoice and due dates, and its branch. Its journal is
    dated on the invoice date when a period covers that day, and on the first day of
    the books otherwise, because nothing can post before the books begin. It is
    refused when dated on or after the day the books went live
    (:func:`books_went_live`); when the books have not gone live yet any date is
    accepted, and the audit row says the date was not checked against one. Once
    posted it is paid, credited, voided and reported like any invoice, and any credit
    the customer already holds pays it (:func:`~vs_finance.receivables.apply_customer_credit`).
    """
    from .account_mappings import resolve_mapped_account
    from .models import Invoice, JournalEntry, JournalLine
    from .posting import post_journal, resolve_period
    from .receivables import apply_customer_credit

    invoice = Invoice.objects.select_for_update(of=("self",)).select_related("customer").get(pk=invoice.pk)
    number = invoice.document_number or invoice.pk
    if invoice.source != InvoiceSource.OPENING:
        raise PostingError(f"Invoice {number} is not an opening invoice.")
    if invoice.status != DocumentStatus.DRAFT:
        raise PostingError(f"Opening invoice {number} is '{invoice.status}'; only a draft can be posted.")
    customer = invoice.customer
    if customer.receivable_account_id is None:
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")
    invoice.recompute_totals(save=True)
    if invoice.total <= 0 or invoice.tax_total:
        raise PostingError("An opening invoice carries a positive amount and no tax.")

    live = books_went_live(invoice.entity)
    if live is not None and invoice.invoice_date >= live:
        raise PostingError(
            f"Bill {invoice.reference or number} for {customer.name} is dated "
            f"{invoice.invoice_date}, on or after the books went live on {live}. "
            f"Raise it as an ordinary invoice instead.",
        )

    journal_date = _opening_journal_date(invoice.entity, invoice.invoice_date)
    equity = resolve_mapped_account(
        invoice.entity, AccountMappingKey.RETAINED_EARNINGS, label="opening balance equity",
    )
    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch, date=journal_date,
        period=resolve_period(invoice.entity, journal_date),
        source=JournalSource.OPENING, currency=invoice.currency,
        narration=invoice.narration or f"Opening balance: {customer.code} {invoice.reference}".strip(),
        reference=invoice.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=customer.receivable_account, debit=invoice.total, credit=0,
        description=f"AR: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=equity, debit=0, credit=invoice.total,
        description="Opening balance", line_no=2,
    )
    post_journal(entry, actor_user=actor_user, allow_control_accounts=True)

    invoice.journal = entry
    invoice.status = DocumentStatus.POSTED
    invoice.refresh_payment_status(save=False)
    invoice.save(update_fields=["journal", "status", "payment_status", "updated_at"])
    record(
        entity=invoice.entity, action=FinanceAuditAction.CUSTOMER_OPENING_POSTED,
        actor_user=actor_user, target=invoice,
        message=(
            f"Carried in an opening invoice for {customer.code} dated {invoice.invoice_date} "
            f"({format_naira(invoice.total)})."
        ),
        journal_id=entry.pk, total=invoice.total, invoice_date=str(invoice.invoice_date),
        journal_date=str(journal_date),
        books_went_live=str(live) if live else None, go_live_checked=live is not None,
    )
    apply_customer_credit(invoice, actor_user=actor_user)
    return invoice


@transaction.atomic
def import_opening_customer_invoices(entity, rows, *, actor_user=None):
    """Create and post one opening invoice per row, all or nothing.

    ``rows`` are dicts carrying ``customer``, ``branch_id``, ``invoice_date``,
    ``due_date``, ``reference``, ``narration``, ``billing_period_label`` and
    ``amount`` (kobo), already resolved and checked for reach by the caller. One
    invoice per unpaid customer bill, not one per customer, is what keeps the ageing
    true.
    """
    from .account_mappings import resolve_mapped_account
    from .models import Invoice, InvoiceLine

    equity = resolve_mapped_account(
        entity, AccountMappingKey.RETAINED_EARNINGS, label="opening balance equity",
    )
    invoices = []
    for row in rows:
        invoice = Invoice.objects.create(
            entity=entity, customer=row["customer"], branch_id=row["branch_id"],
            invoice_date=row["invoice_date"], due_date=row.get("due_date") or row["invoice_date"],
            source=InvoiceSource.OPENING, reference=row.get("reference", ""),
            billing_period_label=row.get("billing_period_label", ""),
            narration=row.get("narration", "") or "Opening balance",
            created_by=actor_user,
        )
        InvoiceLine.objects.create(
            invoice=invoice, description="Opening balance", revenue_account=equity,
            quantity=1, unit_price=int(row["amount"]),
            net_amount=int(row["amount"]), tax_amount=0, line_no=1,
        )
        invoices.append(post_opening_invoice(invoice, actor_user=actor_user))
    return invoices
