"""Fee structures → invoices: the billing layer that turns a fee catalogue into real AR.

A :class:`~vs_finance.models.FeeStructure` is a reusable template of charges
(:class:`~vs_finance.models.FeeItem` lines). :func:`generate_invoices` materialises one
posted :class:`~vs_finance.models.Invoice` per selected customer from that template - the
only place a fee structure becomes money owed. Each generated invoice is posted through the
normal AR path (:func:`vs_finance.receivables.post_invoice`), so it raises the usual
Dr Accounts Receivable / Cr Revenue (+ output tax) journal and shows up everywhere invoices
already do. Money is integer kobo.
"""
from __future__ import annotations

import datetime

from django.db import transaction

from vs_config.clock import branch_today

from .constants import ChargeKind, DocumentStatus
from .exceptions import FinanceError, PostingError
from .receivables import post_invoice


def billing_key_for(structure, billing_period: str = "") -> str:
    """The key a fee run stamps on each invoice: ``FEE:<code>``, ``@<period>`` when named."""
    reference = f"FEE:{structure.code}"
    return f"{reference}@{billing_period}" if billing_period else reference


def already_billed_customer_ids(structure, customer_ids, billing_period: str = "") -> set:
    """Customers holding a live invoice this run would duplicate.

    With a billing period, a structure bills a customer once per period: a live
    invoice carrying the same billing key. Without one, it bills a customer once
    for good: any live invoice from the structure, whichever period it named. That
    second rule is what stops a run from the finance screen (which names no period)
    billing a child the school's own run already billed for this term.
    """
    from .models import Invoice

    live = Invoice.objects.filter(
        entity=structure.entity, customer_id__in=list(customer_ids),
    ).exclude(status__in=(DocumentStatus.REVERSED, DocumentStatus.CANCELLED))
    if billing_period:
        live = live.filter(billing_key=billing_key_for(structure, billing_period))
    else:
        reference = f"FEE:{structure.code}"
        live = live.filter(reference=reference)
    return set(live.values_list("customer_id", flat=True))


@transaction.atomic
# Handle the generate invoices workflow.
def generate_invoices(structure, customers, *, invoice_date=None, due_date=None,
                      actor_user=None, branch=None, invoice_branches=None,
                      billing_period="", billing_period_label="", service_start=None,
                      service_end=None):  # Generate posted invoices from a fee structure.
    """Raise one posted invoice per customer from ``structure``'s fee items.

    ``customers`` is an iterable of :class:`~vs_finance.models.Customer`. Returns the list
    of created (POSTED) invoices. Raises :class:`PostingError` if the structure is empty
    or inactive.

    **A run cannot bill twice.** The structure row is locked for the length of the run,
    so two runs of one structure (a double click, a retry after a slow response, the
    finance screen and the school screen at once) queue rather than interleave, and the
    second finds the first's invoices and skips those customers
    (:func:`already_billed_customer_ids`). Each invoice carries a ``billing_key`` that is
    unique per customer among live invoices in the database, so a run that somehow
    missed the lock fails instead of billing again.

    ``billing_period`` and ``billing_period_label`` are the period the owner layer
    bills by (a school's term), stamped on every invoice and fixed once it posts. A
    structure reused for a later period bills again for that period; reports read the
    period from the invoice, never from how the structure is linked today.

    **Who is billed.** An inactive customer is skipped: one who has left keeps their
    debt but is billed no more. An optional item is billed only to the customers
    assigned to it (:class:`~vs_finance.models.FeeItemAssignment`), and a customer
    left with no line at all gets no invoice.

    An omitted ``due_date`` is derived here, from the entity's
    ``default_invoice_due_days``, rather than left null. A null due date does not
    read as "no deadline", it reads as "never overdue": every ageing bucket,
    every debtor list and every dunning run selects on ``due_date__lt``, and NULL
    matches none of them. Deriving it at this level rather than in a caller is what
    makes that true for every caller, including the owner layer's bridge, which bills
    a cohort and passes no date.

    **Every invoice names a branch.** It is, in order: the branch
    ``invoice_branches`` names for that customer (``{customer id: branch id}``),
    the customer's own branch, then ``branch``, the one branch a run gives every
    customer the branches share. An owner layer that knows each customer's branch
    better than the customer record does (a school knows each pupil's) names it
    per customer, and the engine refuses a name that contradicts a customer's own
    branch rather than choosing between the two. An invoice left with no branch
    is refused, so a run never files a bill under no branch by omission. Every
    tenant owns a branch, so at a tenant that owns none the refusal is
    :class:`~vs_finance.exceptions.BranchlessTenantError`, a data fault, rather
    than a request to name one.

    An omitted ``invoice_date`` is today at the branch each invoice is raised
    for, so a family billed at a branch that keeps its own time zone is billed on
    that branch's day, and its due date counts from it.

    ``service_start`` / ``service_end`` are the dates the billed service runs over,
    stamped on every charge line (a deposit is held, not earned, so its lines carry
    none). A run billed before the service starts posts deferred income that is
    released month by month (:mod:`vs_finance.deferred_income`). Both or neither.
    """
    from .models import FeeItemAssignment, FeeStructure, Invoice, InvoiceLine

    # One run of a structure at a time; see the docstring.
    structure = FeeStructure.objects.select_for_update(of=("self",)).select_related(
        "entity", "entity__tenant").get(pk=structure.pk)
    items = list(structure.items.select_related("revenue_account", "tax_code").all())
    if not items:  # A structure with no items cannot produce an invoice.
        raise PostingError(f"Fee structure {structure.code} has no items to bill.")
    if not structure.is_active:  # Inactive structures must not be billed.
        raise PostingError(f"Fee structure {structure.code} is inactive.")
    if (service_start is None) != (service_end is None):
        raise PostingError("A service period needs both its first and its last day.")
    if service_start is not None and service_end < service_start:
        raise PostingError("A service period cannot end before it starts.")

    due_after = None
    if due_date is None:  # Never leave it null: null is not a deadline, it is never overdue.
        from .document_settings import resolve_finance_document_settings
        policy = resolve_finance_document_settings(structure.entity)
        due_after = datetime.timedelta(days=policy.default_invoice_due_days)
    reference = f"FEE:{structure.code}"  # What a statement shows for this structure.
    billing_key = billing_key_for(structure, billing_period)

    customers = list(customers)
    for customer in customers:  # Cross-entity billing would corrupt books.
        if customer.entity_id != structure.entity_id:
            raise FinanceError(
                f"Customer {customer.code} is not in entity {structure.entity.code}.")
    named = {cid: getattr(b, "pk", b) for cid, b in (invoice_branches or {}).items()}
    shared_branch_id = getattr(branch, "pk", branch)
    billed = already_billed_customer_ids(
        structure, [customer.pk for customer in customers], billing_period)
    optional_ids = [item.pk for item in items if item.is_optional]
    takes = set(
        FeeItemAssignment.objects.filter(
            item_id__in=optional_ids, customer_id__in=[c.pk for c in customers],
        ).values_list("item_id", "customer_id")
    ) if optional_ids else set()
    created = []  # Collect generated posted invoices for the return value.

    for customer in customers:  # Generate at most one invoice per selected customer.
        if customer.pk in billed or not customer.is_active:  # Billed already, or left.
            continue
        lines = [
            item for item in items
            if not item.is_optional or (item.pk, customer.pk) in takes
        ]
        if not lines:  # Nothing on this structure applies to this customer.
            continue

        branch_id = _invoice_branch_id(customer, named, shared_branch_id)
        if branch_id is None:  # A transaction always names a branch.
            from .branch_ledger import only_branch_id_or_several
            only_branch_id_or_several(structure.entity.tenant_id)  # Raises for a tenant with none.
            raise PostingError(
                f"Customer {customer.code} is shared by every branch, so say "
                f"which branch their {structure.code} invoice belongs to.")
        dated = invoice_date or branch_today(structure.entity.tenant, branch_id)
        invoice = Invoice.objects.create(
            entity=structure.entity, customer=customer,  # Scope invoice to the structure entity and customer.
            branch_id=branch_id,
            invoice_date=dated, due_date=due_date or dated + due_after,  # Billing and due dates.
            source="MANUAL", reference=reference,  # Mark source and statement reference.
            billing_key=billing_key, billing_period=billing_period or "",
            billing_period_label=billing_period_label or "",
            narration=f"{structure.name} ({structure.code})",  # Describe the generated fee bill.
            created_by=actor_user,  # Attribute creation to the caller.
        )
        for item in lines:  # Materialize each applicable fee item as an invoice line.
            InvoiceLine.objects.create(
                invoice=invoice, line_no=item.line_no or 0,  # Preserve configured line ordering.
                description=item.description,  # Copy fee item description.
                revenue_account=item.revenue_account,  # Copy the revenue posting account.
                quantity=1, unit_price=item.amount,  # Bill one unit at the configured kobo amount.
                tax_code=item.tax_code,  # Copy configured output tax code.
                kind=item.kind,
                service_start=None if item.kind == ChargeKind.DEPOSIT else service_start,
                service_end=None if item.kind == ChargeKind.DEPOSIT else service_end,
            )
        post_invoice(invoice, actor_user=actor_user)  # Price, validate, and post the invoice to AR/GL.
        invoice.refresh_from_db()
        created.append(invoice)  # Include the posted invoice in the result list.

    return created  # Return all newly created posted invoices.


def _invoice_branch_id(customer, named, shared_branch_id):
    """The branch id a fee invoice for *customer* names; see :func:`generate_invoices`."""
    if customer.pk in named:
        branch_id = named[customer.pk]
        if customer.branch_id is not None and customer.branch_id != branch_id:
            raise FinanceError(
                f"Customer {customer.code} is filed under another branch than the "
                f"one named for their invoice.")
        return branch_id
    if customer.branch_id is not None:
        return customer.branch_id
    return shared_branch_id
