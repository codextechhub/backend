"""Fee structures → invoices: the billing layer that turns a fee catalogue into real AR.

A :class:`~vs_finance.models.FeeStructure` is a reusable template of charges
(:class:`~vs_finance.models.FeeItem` lines). :func:`generate_invoices` materialises one
posted :class:`~vs_finance.models.Invoice` per selected customer from that template — the
only place a fee structure becomes money owed. Each generated invoice is posted through the
normal AR path (:func:`vs_finance.receivables.post_invoice`), so it raises the usual
Dr Accounts Receivable / Cr Revenue (+ output tax) journal and shows up everywhere invoices
already do. Money is integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import transaction

from .exceptions import FinanceError, PostingError
from .receivables import post_invoice


@transaction.atomic
def generate_invoices(structure, customers, *, invoice_date=None, due_date=None,
                      actor_user=None):
    """Raise one posted invoice per customer from ``structure``'s fee items.

    ``customers`` is an iterable of :class:`~vs_finance.models.Customer`. Returns the list
    of created (POSTED) invoices. Skips a customer who already has a posted invoice
    referencing this structure (idempotent re-run guard via the invoice ``reference``).
    Raises :class:`PostingError` if the structure is empty or inactive.
    """
    from .models import Invoice, InvoiceLine

    items = list(structure.items.select_related("revenue_account", "tax_code").all())
    if not items:
        raise PostingError(f"Fee structure {structure.code} has no items to bill.")
    if not structure.is_active:
        raise PostingError(f"Fee structure {structure.code} is inactive.")

    invoice_date = invoice_date or datetime.date.today()
    reference = f"FEE:{structure.code}"
    created = []

    for customer in customers:
        if customer.entity_id != structure.entity_id:
            raise FinanceError(
                f"Customer {customer.code} is not in entity {structure.entity.code}.")
        # Idempotency: don't double-bill the same structure to the same customer.
        if Invoice.objects.filter(
            entity=structure.entity, customer=customer, reference=reference,
            status="POSTED",
        ).exists():
            continue

        invoice = Invoice.objects.create(
            entity=structure.entity, customer=customer,
            invoice_date=invoice_date, due_date=due_date,
            source="MANUAL", reference=reference,
            narration=f"{structure.name} ({structure.code})",
            created_by=actor_user,
        )
        for item in items:
            InvoiceLine.objects.create(
                invoice=invoice, line_no=item.line_no or 0,
                description=item.description,
                revenue_account=item.revenue_account,
                quantity=1, unit_price=item.amount,
                tax_code=item.tax_code,
            )
        post_invoice(invoice, actor_user=actor_user)
        invoice.refresh_from_db()
        created.append(invoice)

    return created
