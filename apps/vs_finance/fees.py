"""Fee structures → invoices: the billing layer that turns a fee catalogue into real AR.

A :class:`~vs_finance.models.FeeStructure` is a reusable template of charges
(:class:`~vs_finance.models.FeeItem` lines). :func:`generate_invoices` materialises one
posted :class:`~vs_finance.models.Invoice` per selected customer from that template — the
only place a fee structure becomes money owed. Each generated invoice is posted through the
normal AR path (:func:`vs_finance.receivables.post_invoice`), so it raises the usual
Dr Accounts Receivable / Cr Revenue (+ output tax) journal and shows up everywhere invoices
already do. Money is integer kobo.
"""
from __future__ import annotations  # Defer annotations and keep module imports light.

import datetime  # Supplies today's date when caller omits invoice_date.

from django.db import transaction  # Keeps fee invoice generation atomic.

from .exceptions import FinanceError, PostingError  # Domain errors for invalid fee billing.
from .receivables import post_invoice  # Posts generated invoices through the normal AR engine.


@transaction.atomic
def generate_invoices(structure, customers, *, invoice_date=None, due_date=None,
                      actor_user=None):  # Generate posted invoices from a fee structure.
    """Raise one posted invoice per customer from ``structure``'s fee items.

    ``customers`` is an iterable of :class:`~vs_finance.models.Customer`. Returns the list
    of created (POSTED) invoices. Skips a customer who already has a posted invoice
    referencing this structure (idempotent re-run guard via the invoice ``reference``).
    Raises :class:`PostingError` if the structure is empty or inactive.
    """
    from .models import Invoice, InvoiceLine  # Local import avoids model import cycles.

    items = list(structure.items.select_related("revenue_account", "tax_code").all())  # Load billable fee lines and posting targets.
    if not items:  # A structure with no items cannot produce an invoice.
        raise PostingError(f"Fee structure {structure.code} has no items to bill.")
    if not structure.is_active:  # Inactive structures must not be billed.
        raise PostingError(f"Fee structure {structure.code} is inactive.")

    invoice_date = invoice_date or datetime.date.today()  # Default billing date to today.
    reference = f"FEE:{structure.code}"  # Stable idempotency reference for this structure.
    created = []  # Collect generated posted invoices for the return value.

    for customer in customers:  # Generate at most one invoice per selected customer.
        if customer.entity_id != structure.entity_id:  # Cross-entity billing would corrupt books.
            raise FinanceError(
                f"Customer {customer.code} is not in entity {structure.entity.code}.")
        # Idempotency: don't double-bill the same structure to the same customer.  # Re-runs are safe.
        if Invoice.objects.filter(  # Look for an already posted invoice for this structure/customer.
            entity=structure.entity, customer=customer, reference=reference,  # Match the same entity, customer, and fee reference.
            status="POSTED",  # Only posted invoices count as already billed.
        ).exists():  # Skip this customer when already billed.
            continue

        invoice = Invoice.objects.create(  # Create the draft customer invoice header.
            entity=structure.entity, customer=customer,  # Scope invoice to the structure entity and customer.
            invoice_date=invoice_date, due_date=due_date,  # Store billing and optional due dates.
            source="MANUAL", reference=reference,  # Mark source and idempotency reference.
            narration=f"{structure.name} ({structure.code})",  # Describe the generated fee bill.
            created_by=actor_user,  # Attribute creation to the caller.
        )
        for item in items:  # Materialize each fee item as an invoice line.
            InvoiceLine.objects.create(  # Create one invoice line for the fee item.
                invoice=invoice, line_no=item.line_no or 0,  # Preserve configured line ordering.
                description=item.description,  # Copy fee item description.
                revenue_account=item.revenue_account,  # Copy the revenue posting account.
                quantity=1, unit_price=item.amount,  # Bill one unit at the configured kobo amount.
                tax_code=item.tax_code,  # Copy configured output tax code.
            )
        post_invoice(invoice, actor_user=actor_user)  # Price, validate, and post the invoice to AR/GL.
        invoice.refresh_from_db()  # Reload posted status, totals, journal, and document number.
        created.append(invoice)  # Include the posted invoice in the result list.

    return created  # Return all newly created posted invoices.
