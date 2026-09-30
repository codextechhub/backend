"""Accounts-Receivable services - the revenue cycle.

Domain-neutral on purpose: these functions move generic invoices and payments into
the General Ledger and never mention students, parents or fees. A billing *source*
(a school fee run, a subscription engine) is just whatever creates the
:class:`~vs_finance.models.Invoice` rows; from here down it's pure double-entry.

The two postings this layer raises:

* **Invoice** → ``Dr receivable control, Cr revenue (per line), Cr output tax``.
* **Payment** → ``Dr bank/cash, Cr receivable control`` - then the cash is *allocated*
  across invoices (a sub-ledger act with no further GL effect).

All amounts are integer kobo; tax is computed from basis points with the same
``ROUND_HALF_UP`` discipline as :mod:`vs_finance.money`.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from vs_config.display import format_date

from .account_mappings import resolve_mapped_account
from .audit import record, record_rejection
from .chronology import ANY_BRANCH
from .constants import (
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
)
from .exceptions import (
    FinanceError, PostingError, SettlementBranchError, SettlementTargetError,
)
from .posting import post_journal, resolve_period


# --------------------------------------------------------------------------- #
# Money helpers (integer kobo)                                                 #
# --------------------------------------------------------------------------- #

# Handle the compute line net workflow.
def compute_line_net(quantity, unit_price_kobo: int) -> int:
    """``quantity × unit_price`` in kobo, rounded half-up to a whole kobo."""
    amount = Decimal(quantity) * Decimal(int(unit_price_kobo))
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# Handle the compute tax workflow.
def compute_tax(net_kobo: int, rate_bps: int) -> int:
    """Tax on ``net_kobo`` at ``rate_bps`` basis points (750 = 7.5%), half-up to kobo.

    Integer-exact: a tax line is never carried as a float.
    """
    if not rate_bps:  # Zero rate means no tax.
        return 0
    amount = Decimal(int(net_kobo)) * Decimal(int(rate_bps)) / Decimal(10000)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# Handle the price invoice workflow.
def price_invoice(invoice) -> None:
    """Compute each line's ``net_amount``/``tax_amount`` and roll up the invoice totals.

    Idempotent: safe to call repeatedly while an invoice is still a draft.
    """
    from .models import InvoiceLine

    for line in invoice.lines.all():  # Reprice each line from the current quantity and unit price.
        net = compute_line_net(line.quantity, line.unit_price)  # Calculate the net line amount.
        rate = line.tax_code.rate_bps if line.tax_code_id else 0  # Use the line's tax rate when available.
        tax = compute_tax(net, rate)  # Derive the tax amount for the line.
        if line.net_amount != net or line.tax_amount != tax:  # Only write when the computed values changed.
            InvoiceLine.objects.filter(pk=line.pk).update(net_amount=net, tax_amount=tax)
    invoice.recompute_totals(save=True)  # Roll the repriced lines up into invoice totals.


# --------------------------------------------------------------------------- #
# Invoice posting                                                             #
# --------------------------------------------------------------------------- #

# Handle the post invoice workflow.
def post_invoice(invoice, *, actor_user=None):
    """Price, validate and post an :class:`Invoice`, raising its AR journal.

    Wrapper that records a durable rejection audit on any :class:`FinanceError`, then
    re-raises - mirroring the journal posting contract.
    """
    try:  # The atomic worker owns the ledger write; this wrapper owns the rejection audit.
        result = _post_invoice_atomic(invoice, actor_user=actor_user)  # Post the invoice into AR and the GL.
    except FinanceError as exc:  # Convert any posting failure into a durable rejection record.
        record_rejection(  # Write a committed rejection audit event.
            entity=invoice.entity,
            action=FinanceAuditAction.INVOICE_POSTED,
            exc=exc, actor_user=actor_user, target=invoice,
        )
        raise
    # Best-effort customer notification (never rolls back the post; skips openings).  # Notify after success only.
    from .notifications import notify_invoice_issued
    notify_invoice_issued(invoice, actor_user=actor_user)  # Send a receipt/notice if configured.
    return result  # Return the posted invoice.


@transaction.atomic
# Support the post invoice atomic workflow.
def _post_invoice_atomic(invoice, *, actor_user=None):
    from .models import JournalEntry, JournalLine

    if invoice.status != DocumentStatus.DRAFT:  # Only drafts can be posted.
        raise PostingError(
            f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}', "
            f"only a draft invoice can be posted.",
        )

    customer = invoice.customer  # The customer drives the AR control account.
    ar_account = customer.receivable_account  # Resolve the customer's AR control account.
    if ar_account is None:  # Posting cannot continue without AR control.
        raise PostingError(
            f"Customer {customer.code} has no receivable account configured. "
            "Open Receivables > Customers, edit this customer, and select an active, "
            "postable Accounts Receivable account before posting an invoice.",
        )

    price_invoice(invoice)  # Ensure all lines and totals are up to date before posting.
    if invoice.total <= 0:  # Reject zero or negative invoices.
        raise PostingError("An invoice must have a positive total to post.")

    period = resolve_period(invoice.entity, invoice.invoice_date)  # Find the open accounting period.

    entry = JournalEntry.objects.create(
        entity=invoice.entity, branch=invoice.branch,
        date=invoice.invoice_date, period=period,
        source=JournalSource.SALES, currency=invoice.currency,
        narration=invoice.narration or f"Invoice {invoice.document_number or ''}".strip(),
        reference=invoice.reference, created_by=actor_user,
    )

    line_no = 0  # Keep line numbers deterministic within the journal.
    # Dr the receivable control for the gross total.  # First line debits AR for the full invoice.
    line_no += 1  # Increment the journal line counter.
    JournalLine.objects.create(
        entry=entry, account=ar_account, debit=invoice.total, credit=0,
        description=f"AR: {customer.code}", line_no=line_no,
    )
    # Cr revenue, grouped by (account, cost centre) so the journal stays tidy while the
    # cost-centre split survives into the GL. Revenue is P&L, so it carries the analytics;
    # the AR control (above) and the output-tax liability (below) do not.
    revenue_by_key: dict[tuple[int, int | None], int] = defaultdict(int)  # Aggregate revenue by account/cost center.
    revenue_objs: dict[tuple[int, int | None], tuple] = {}  # Keep the account/cost-center objects for each key.
    tax_by_account: dict[int, int] = defaultdict(int)  # Aggregate output tax by tax account.
    tax_objs: dict[int, object] = {}  # Keep the tax account objects for each key.
    for line in invoice.lines.select_related(
        "revenue_account", "tax_code__collected_account", "cost_center",
    ):
        key = (line.revenue_account_id, line.cost_center_id)  # Group revenue by account and cost center.
        revenue_by_key[key] += line.net_amount  # Accumulate the net line amount into the group.
        revenue_objs[key] = (line.revenue_account, line.cost_center)  # Keep the objects needed when creating journal lines.
        if line.tax_amount:  # Only tax-bearing lines contribute to output tax.
            tax_acc = line.tax_code.collected_account if line.tax_code_id else None  # Resolve the output tax account.
            if tax_acc is None:  # A taxable line must have a collected account.
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no collected (output) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount  # Accumulate tax by output-tax account.
            tax_objs[tax_acc.id] = tax_acc  # Keep the account object for line creation.

    for (acc_id, cc_id), amount in revenue_by_key.items():  # Emit one revenue line per grouped key.
        if amount == 0:  # Skip zero-value revenue groups.
            continue
        line_no += 1  # Advance the journal line number.
        revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]  # Retrieve the grouped objects.
        JournalLine.objects.create(
            entry=entry, account=revenue_account, debit=0, credit=amount,
            description="Revenue", cost_center=cost_center, line_no=line_no,
        )
    for acc_id, amount in tax_by_account.items():  # Emit one output-tax line per tax account.
        line_no += 1  # Advance the journal line number.
        JournalLine.objects.create(
            entry=entry, account=tax_objs[acc_id], debit=0, credit=amount,
            description="Output tax", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)  # Validate and mark the journal as posted.

    invoice.journal = entry  # Link the invoice to the posted journal.
    invoice.status = DocumentStatus.POSTED  # Mark the invoice as posted.
    invoice.refresh_payment_status(save=False)  # Recompute payment status from allocations.
    invoice.save(update_fields=["journal", "status", "payment_status", "updated_at"])

    record(  # Record the successful invoice posting in the finance audit log.
        entity=invoice.entity, action=FinanceAuditAction.INVOICE_POSTED,
        actor_user=actor_user, target=invoice,
        message=f"Posted invoice for {customer.code} ({invoice.total} kobo).",
        journal_id=entry.pk, total=invoice.total, tax=invoice.tax_total,
    )
    apply_customer_credit(invoice, actor_user=actor_user)  # Credit already held pays the new bill.
    return invoice  # Return the posted invoice.


def apply_customer_credit(invoice, *, actor_user=None) -> int:
    """Settle a newly posted invoice from the customer's unapplied credit.

    Money a customer already holds as credit (a payment made before the bill, an
    overpayment, an unapplied credit note, a credit transfer) pays each new bill of
    theirs as it posts, oldest credit first, so Mr Chukwu, who paid N420,000 in
    August, is not shown as owing N420,000 when September's bill is raised. Only
    credit of the invoice's own branch is used (the settlement rule of
    :func:`_build_invoice_plan`), and each lot is applied through
    :func:`allocate_payment` / :func:`~vs_finance.credit_notes.allocate_credit_note`,
    so the reclassification journal, its date (the later of credit and bill) and its
    audit row are exactly those of a bursar pressing *allocate*.

    The entity's ``auto_apply_customer_credit`` setting turns this off, for books
    that hold money on account on purpose. Returns the kobo applied.
    """
    from .chronology import credit_lots
    from .document_settings import resolve_finance_document_settings
    from .models import CreditNote, Payment

    if invoice.status != DocumentStatus.POSTED or invoice.balance_due <= 0:
        return 0
    if not resolve_finance_document_settings(invoice.entity).auto_apply_customer_credit:
        return 0
    lots = credit_lots(
        invoice.entity, [invoice.customer_id], branch=invoice.branch_id,
    ).get(invoice.customer_id, [])
    applied = 0
    for lot in lots:
        if invoice.balance_due <= 0:  # The bill is paid.
            break
        take = min(lot.remaining, invoice.balance_due)
        if lot.kind == "RECEIPT":
            source = Payment.objects.get(pk=lot.document_id)
            rows = allocate_payment(source, allocations=[(invoice, take)], actor_user=actor_user)
        else:
            from .credit_notes import allocate_credit_note

            source = CreditNote.objects.get(pk=lot.document_id)
            rows = allocate_credit_note(source, allocations=[(invoice, take)], actor_user=actor_user)
        applied += sum(int(row.amount) for row in rows)
        invoice.refresh_from_db()
    return applied


# Handle the post opening balance workflow.
def post_opening_balance(customer, *, actor_user=None, date=None, branch_id=None):
    """Seat a customer's ``opening_balance`` as a posted opening invoice.

    Raises an :class:`~vs_finance.models.Invoice` (``source=OPENING``) that posts
    ``Dr AR control · Cr Retained Earnings`` - the opening figure is prior-period
    value, so it credits **equity**, never current-period revenue (crediting
    revenue would overstate the income statement every time a customer is migrated
    in with a balance). It still shows in the customer's outstanding (which is
    invoice-derived) *and* in the GL. No-op unless the opening balance is positive.
    Returns the invoice or ``None``. Runs the normal :func:`post_invoice` guards
    (open period, etc.).

    The invoice belongs to the customer's branch. ``branch_id`` is the branch for a
    customer every branch shares, who has none to give: the invoice is a
    transaction, and names a branch.
    """
    from vs_config.clock import branch_today

    from .constants import InvoiceSource
    from .models import Invoice, InvoiceLine

    amount = int(customer.opening_balance or 0)  # Normalize the starting balance to integer kobo.
    if amount <= 0:  # Skip zero or negative opening balances.
        return None

    # Opening balances are prior-period value: credit equity (Retained Earnings),
    # not revenue - otherwise onboarding/migrating a customer inflates this year's P&L.
    opening_equity = resolve_mapped_account(  # Book the offset to retained earnings.
        customer.entity, AccountMappingKey.RETAINED_EARNINGS, label="opening balance equity",
    )
    invoice_branch_id = customer.branch_id or branch_id
    invoice = Invoice.objects.create(
        entity=customer.entity, customer=customer,
        branch_id=invoice_branch_id,  # Opening balance belongs where the customer does.
        invoice_date=date or branch_today(customer.entity.tenant, invoice_branch_id),
        source=InvoiceSource.OPENING,
        narration=f"Opening balance for {customer.code}",
        created_by=actor_user,
    )
    InvoiceLine.objects.create(
        invoice=invoice, revenue_account=opening_equity,
        quantity=1, unit_price=amount, line_no=1,
    )
    post_invoice(invoice, actor_user=actor_user)  # Post the synthetic invoice into the GL.
    return invoice  # Return the posted opening-balance invoice.


# --------------------------------------------------------------------------- #
# Payment posting + allocation                                                #
# --------------------------------------------------------------------------- #

# Handle the post payment workflow.
def post_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                 strategy="oldest"):
    """Post a customer :class:`Payment` (Dr bank, Cr AR) and allocate it to invoices.

    ``allocations`` (a list of ``(invoice, amount_kobo)``) applies an explicit split;
    otherwise ``auto_allocate`` settles open invoices in ``strategy`` order
    (``"oldest"`` by due date, or ``"largest"`` balance first). Either way only
    documents of the receipt's own branch are settled (see :func:`_build_invoice_plan`).
    """
    try:  # The atomic worker owns the ledger write and allocation updates.
        result = _post_payment_atomic(  # Post the receipt and optionally allocate it.
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations, strategy=strategy,
        )
    except FinanceError as exc:  # Convert posting failures into durable rejection audit.
        record_rejection(  # Write a committed rejection event.
            entity=payment.entity,
            action=FinanceAuditAction.PAYMENT_POSTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise
    # Queue the receipt confirmation only after the ledger transaction commits.  Email
    # setup/template rendering must not hold the payment API response open, and a rolled
    # back payment must never leave behind a customer notification.
    from django.db import transaction
    from .tasks import queue_payment_received_notification

    actor_user_id = getattr(actor_user, "pk", None)
    transaction.on_commit(
        lambda: queue_payment_received_notification(payment.pk, actor_user_id=actor_user_id),
    )
    return result  # Return the posted payment.


# Handle customer credit balances in bulk so list screens and posting guards share
# one definition without introducing a query per customer.
def customer_credit_balances(entity, customer_ids=None, *, as_of=None,
                             scope=None) -> dict[int, int]:
    """Return customer-credit (2140) balances keyed by customer id.

    Credit is the sum of the customer's :class:`~vs_finance.chronology.CreditLot`
    parcels - unapplied receipts and unapplied CREDIT notes, each already net of what
    has been refunded back out of it or transferred to another customer. Value leaves
    2140 by an allocation: one a person makes, or the one
    :func:`apply_customer_credit` makes when a new invoice posts.

    ``as_of`` restricts the sum to credit that **existed on that accounting date**:
    a receipt dated later has not happened yet as far as that date is concerned and
    cannot fund anything. Without it, the answer is "right now", which is exactly the
    assumption that let backdated refunds spend credit from the future.

    Note the deliberate asymmetry: lots are filtered by date, but the deductions
    inside a lot (allocations, prior refunds) are not. That is the safe direction -
    value that has since been spent stays spent, so an as-of figure can only ever be
    conservative and never authorises paying the same kobo out twice.

    ``scope`` narrows the lots to a reader's branches (see
    :func:`~vs_finance.chronology.credit_lots`). It is for display only: a guard
    deciding whether credit exists must read the whole entity.
    """
    from .chronology import credit_lots

    lots = credit_lots(entity, customer_ids, as_of=as_of, scope=scope)
    return {
        customer_id: sum(lot.remaining for lot in customer_lots)
        for customer_id, customer_lots in lots.items()
    }


# Handle the customer credit balance workflow.
def customer_credit_balance(customer, *, as_of=None) -> int:
    """A customer's stored credit in kobo, optionally as at an accounting date."""
    return customer_credit_balances(
        customer.entity, [customer.pk], as_of=as_of).get(customer.pk, 0)


def customer_refund_available_balances(
    entity, customer_ids=None, *, exclude_refund_id=None, as_of=None, branch=ANY_BRANCH,
) -> dict[int, int]:
    """Return credit still available for a new refund request, keyed by customer id.

    Three deductions sit between stored credit and refundable credit:

    * **Unsettled DEBIT notes** - the customer still owes these, so their value is
      not handed back as cash. (Open *invoices* deliberately do not reduce it; only
      an explicit allocation settles those.)
    * **Pending approvals** reserve their amount, so two requests cannot promise the
      same credit. Drafts deliberately do not reserve; they are revalidated on submit
      and again on post.
    * **``as_of``** - credit that does not yet exist on the refund's own accounting
      date cannot fund it. Callers must pass the refund date, not today.

    ``branch`` measures one branch's credit, debit notes and reservations only (a
    branch id, compared by :func:`vs_rbac.scoping.same_transaction_branch`), which
    is what a refund of that branch can pay out.
    :data:`~vs_finance.chronology.ANY_BRANCH` reads every branch together.
    """
    return _refund_available(
        entity, customer_ids, exclude_refund_id=exclude_refund_id, as_of=as_of,
        branch=branch, by_branch=False,
    )


def refundable_credit_by_branch(entity, customer_ids=None, *, as_of=None,
                                scope=None) -> dict[tuple[int, int | None], int]:
    """Refundable credit per ``(customer id, branch id)``, positive amounts only.

    One refund pays out one branch's credit, so this is the figure a refund screen
    offers: a family holding 30,000 at Ikeja and 20,000 at Lekki has two refundable
    amounts, not one of 50,000. ``scope`` (a :class:`vs_rbac.scoping.BranchScope`)
    keeps the branches a reader may refund from; credit not yet given a branch is
    kept only for a reader the scope does not narrow.
    """
    available = _refund_available(
        entity, customer_ids, exclude_refund_id=None, as_of=as_of,
        branch=ANY_BRANCH, by_branch=True,
    )
    if any(branch_id is None for _cid, branch_id in available):
        available = _unbranched_credit_to_the_only_branch(entity, available)
    ids = None if scope is None else scope.branch_ids
    shared = scope is None or scope.include_shared
    return {
        key: amount for key, amount in available.items()
        if amount > 0 and (ids is None or key[1] in ids or (key[1] is None and shared))
    }


def _unbranched_credit_to_the_only_branch(entity, available):
    """Fold credit not yet given a branch into the tenant's only branch, where it has one.

    At a school with one branch that credit is the branch's
    (:func:`vs_rbac.scoping.same_transaction_branch`), and a refund raised there
    names the branch, so the figure it is offered must be keyed the same way.
    """
    from vs_rbac.scoping import only_branch_id

    only = only_branch_id(entity.tenant_id)
    if only is None:
        return available
    folded: dict = defaultdict(int)
    for (customer_id, branch_id), amount in available.items():
        folded[(customer_id, only if branch_id is None else branch_id)] += amount
    return dict(folded)


def _refund_available(entity, customer_ids, *, exclude_refund_id, as_of, branch, by_branch):
    """Refundable credit keyed by customer, or by ``(customer, branch)`` with ``by_branch``."""
    from django.db.models import F, Sum
    from django.db.models.functions import Coalesce

    from .chronology import credit_lots
    from .constants import CreditNoteKind
    from .models import CreditNote, Refund

    def key(customer_id, branch_id):
        return (customer_id, branch_id) if by_branch else customer_id

    credit: dict = defaultdict(int)
    for customer_id, lots in credit_lots(
            entity, customer_ids, as_of=as_of, branch=branch).items():
        for lot in lots:
            credit[key(customer_id, lot.branch_id)] += lot.remaining

    debit_notes = CreditNote.objects.filter(
        entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)
    pending = Refund.objects.filter(
        entity=entity, status=DocumentStatus.PENDING_APPROVAL)
    if customer_ids is not None:
        customer_ids = list(customer_ids)
        debit_notes = debit_notes.filter(customer_id__in=customer_ids)
        pending = pending.filter(customer_id__in=customer_ids)
    if branch is not ANY_BRANCH:
        from vs_rbac.scoping import transaction_branch_match_q

        held_here = transaction_branch_match_q(entity.tenant_id, branch)
        debit_notes, pending = debit_notes.filter(held_here), pending.filter(held_here)
    if exclude_refund_id is not None:
        pending = pending.exclude(pk=exclude_refund_id)

    owed: dict = defaultdict(int)
    for row in debit_notes.values("customer_id", "branch_id").annotate(
            amount=Coalesce(Sum(F("total") - F("amount_paid")), 0)):
        owed[key(row["customer_id"], row["branch_id"])] += int(row["amount"] or 0)
    reserved: dict = defaultdict(int)
    for row in pending.values("customer_id", "branch_id").annotate(
            amount=Coalesce(Sum("amount"), 0)):
        reserved[key(row["customer_id"], row["branch_id"])] += int(row["amount"] or 0)

    return {
        k: max(0, credit.get(k, 0) - owed.get(k, 0) - reserved.get(k, 0))
        for k in set(credit) | set(owed) | set(reserved)
    }


def customer_refund_available_balance(customer, *, exclude_refund_id=None, as_of=None,
                                      branch=ANY_BRANCH) -> int:
    """Credit available to one refund on its own date, after pending reservations.

    ``branch`` is the refund's own branch id; see
    :func:`customer_refund_available_balances`.
    """
    return customer_refund_available_balances(
        customer.entity, [customer.pk],
        exclude_refund_id=exclude_refund_id, as_of=as_of, branch=branch,
    ).get(customer.pk, 0)


def require_refund_branch_credit(customer, amount, branch_id, *, as_of=None,
                                 exclude_refund_id=None):
    """Refuse a refund its own branch cannot fund while another branch holds the credit.

    A refund pays out only the credit of its own branch (see
    :func:`~vs_finance.chronology.credit_lots`). When that falls short but the
    customer has refundable credit elsewhere, the ordinary "exceeds available credit"
    message would read as a missing balance; this names both sides instead, for
    example "This refund belongs to Ikeja Branch and OKAFOR's refundable credit of
    ₦300.00 is held by Lekki Branch. Raise the refund for Lekki Branch."
    Returns quietly when the own branch covers ``amount`` or no other branch holds
    anything, so the caller's own shortfall message still applies.
    """
    from vs_rbac.scoping import same_transaction_branch
    from vs_tenants.models import Branch

    from .money import format_naira

    own = customer_refund_available_balance(
        customer, exclude_refund_id=exclude_refund_id, as_of=as_of,
        branch=branch_id,
    )
    if amount <= own:
        return
    tenant_id = customer.entity.tenant_id
    elsewhere = {
        branch: held for (_cid, branch), held in _refund_available(
            customer.entity, [customer.pk], exclude_refund_id=exclude_refund_id,
            as_of=as_of, branch=ANY_BRANCH, by_branch=True,
        ).items()
        if held > 0 and not same_transaction_branch(tenant_id, branch, branch_id)
    }
    if not elsewhere:
        return
    names = dict(Branch.objects.filter(
        pk__in=[b for b in (*elsewhere, branch_id) if b]).values_list("pk", "name"))
    side = (f"belongs to {names[branch_id]}" if branch_id is not None
            else "has not been given a branch")
    branched = sorted((b for b in elsewhere if b is not None), key=lambda b: names.get(b, ""))
    if not branched:
        held = format_naira(sum(elsewhere.values()))
        raise SettlementBranchError(
            f"This refund {side} and {customer.code}'s refundable credit of {held} has "
            f"not been given a branch, so no branch's refund can pay it out."
        )
    held = format_naira(sum(elsewhere[b] for b in branched))
    holders = " and ".join(names[b] for b in branched)
    raise SettlementBranchError(
        f"This refund {side} and {customer.code}'s refundable credit of {held} is "
        f"held by {holders}. Raise the refund for {names[branched[0]]}."
    )


#: Supported auto-allocation strategies for settling a receipt's cash.  # Keep strategy names explicit and small.
ALLOCATION_STRATEGIES = ("oldest", "largest")


def _build_invoice_plan(source, allocations, *, strategy="oldest", include_debit_notes=False,
                        as_of=None, settlement="This settlement"):
    """An explicit ``[(target, amount)]`` plan, or open AR items in ``strategy`` order.

    ``source`` is the document whose value settles the plan: a :class:`Payment` or a
    CREDIT :class:`CreditNote`. Its customer names whose AR items are settled, and its
    branch names which of them: only items of the source's own branch
    (:func:`vs_rbac.scoping.same_transaction_branch`). The settling journal is booked
    to the source's branch while each item's receivable sits on the item's branch, so Ikeja's receipt
    settling a Lekki invoice would clear Lekki's debt out of Ikeja's books. The
    automatic plan draws only from those items; an explicit plan naming any other is
    refused before anything is settled (see :func:`_require_settlable_targets`),
    because a caller who covers both branches can name either. The same check refuses
    a target that is not posted or belongs to another customer.

    A *target* is an :class:`Invoice` or - when ``include_debit_notes`` is set - a posted
    DEBIT :class:`CreditNote`, which debits AR just like an invoice and is settled the
    same way by receipts. Both expose ``balance_due``. ``strategy`` (when ``allocations``
    is not given): ``"oldest"`` settles by document date first (the default), ``"largest"``
    settles the biggest outstanding balance first. Debit-note settlement is opt-in because
    the credit-note sub-ledger can only point at invoices; payment paths pass it True.

    ``as_of`` is the settling document's own accounting date, and it is the choke point
    for causal ordering on the settlement side: a receipt dated 1 Sep cannot clear a bill
    raised on 9 Sep, because on 1 Sep the receivable does not exist and crediting AR would
    drive the control account negative for the gap.

    The two modes handle that differently, on purpose:

    * **Auto-allocation** silently *skips* not-yet-raised targets. This is not a
      failure - it is a prepayment, and the cash correctly falls through to the
      customer-credit liability to be applied when the bill arrives.
    * **An explicit plan** names a target the user chose, so silently dropping it
      would post something other than what was asked for. It raises instead, and
      says which date to use.
    """
    from .chronology import accounting_date, describe, ensure_on_or_after
    from .constants import CreditNoteKind
    from .models import CreditNote, Invoice

    if allocations is not None:  # Explicit allocations always win over auto-allocation.
        plan = list(allocations)  # Normalize to a list so the caller can iterate safely.
        _require_settlable_targets(source, [target for target, _amount in plan])
        if as_of is not None:  # An explicitly named target must already exist on that date.
            for target, _amount in plan:
                target_date = accounting_date(target)
                ensure_on_or_after(
                    subject=settlement, subject_date=as_of,
                    source=describe(target, "the document being settled"),
                    source_date=target_date,
                    remedy=(
                        f"Either date it {format_date(target_date, source.entity.tenant)} "
                        f"or later, or leave the money unallocated as customer credit and "
                        f"apply it once that document exists."
                    ),
                    tenant=source.entity.tenant,
                )
        return plan  # Explicit plan passed its branch and date checks.

    from vs_rbac.scoping import transaction_branch_match_q

    same_branch = transaction_branch_match_q(source.entity.tenant_id, source.branch_id)
    own = {"customer_id": source.customer_id, "status": DocumentStatus.POSTED}
    open_invoices = list(  # Open posted invoices of the customer, in the source's branch.
        Invoice.objects.filter(same_branch, **own)
        .exclude(payment_status=InvoicePaymentStatus.PAID)
    )
    if as_of is not None:  # Auto-allocation only settles what already exists.
        open_invoices = [inv for inv in open_invoices if inv.invoice_date <= as_of]
    # (target, balance_due, sort_date) - sort_date drives oldest-first across both types.
    items = [(inv, inv.balance_due, inv.due_date or inv.invoice_date) for inv in open_invoices]  # Invoice settlement candidates.
    if include_debit_notes:  # Optionally include posted debit notes in the settlement plan.
        open_notes = list(  # Open debit notes of the customer, in the source's branch.
            CreditNote.objects.filter(same_branch, kind=CreditNoteKind.DEBIT, **own)
            .exclude(settlement_status=InvoicePaymentStatus.PAID)
        )
        if as_of is not None:  # Same rule: a charge not yet raised cannot be settled.
            open_notes = [dn for dn in open_notes if dn.note_date <= as_of]
        items += [(dn, dn.balance_due, dn.note_date) for dn in open_notes]  # Add debit notes to the same plan.

    if strategy == "largest":  # Largest-balance-first strategy.
        items.sort(key=lambda t: (-t[1], t[2], t[0].pk))  # Sort by balance descending, then date, then pk.
    else:  # Default is oldest-first.
        items.sort(key=lambda t: (t[2], t[0].pk))  # Sort by document date, then pk.
    return [(target, balance) for target, balance, _date in items]  # Strip the sort date before returning.


def _require_settlable_targets(source, targets):
    """Refuse any target ``source``'s value may not settle, before anything is settled.

    The one place every settlement target is judged, for an automatic plan and one a
    person names alike, and again after the targets are locked (a bill voided while
    a receipt was being keyed is caught there). A target must be:

    * **posted**. A draft owes nothing yet, and a voided invoice keeps its full
      balance: cash applied to it would credit AR for a debt that no longer exists
      and vanish from the customer's credit. For example "Invoice INV-0412 has been
      voided, so nothing can be settled against it. Leave the money as customer
      credit, or apply it to the invoice that replaced it."
    * **the source's own customer's**. Mrs Eze's transfer applied to the Bello
      family's bill would mark the Bellos paid with money that is not theirs. Money
      moves between customers only through an approved customer credit transfer
      (:class:`~vs_finance.models.CustomerCreditTransfer`).
    * **an invoice, when the source is a credit note**. A credit note's sub-ledger
      points at invoices only; a debit note is settled by receipts.
    * **of the source's own branch** (:func:`vs_rbac.scoping.same_transaction_branch`),
      for example "This receipt belongs to Ikeja Branch and invoice INV-0002 belongs
      to Lekki Branch. Apply it to an Ikeja Branch invoice."
    """
    from vs_rbac.scoping import same_transaction_branch

    from .models import CreditNote

    source_is_note = isinstance(source, CreditNote)
    noun = "credit note" if source_is_note else "receipt"
    for target in targets:
        kind = "debit note" if isinstance(target, CreditNote) else "invoice"
        number = target.document_number or f"the selected {kind}"
        if source_is_note and isinstance(target, CreditNote):
            raise SettlementTargetError(
                f"Debit note {number} is settled by a receipt; a credit note settles "
                f"invoices only.",
            )
        if target.status != DocumentStatus.POSTED:
            if target.status == DocumentStatus.REVERSED:
                raise SettlementTargetError(
                    f"{kind.capitalize()} {number} has been voided, so nothing can be "
                    f"settled against it. Leave the money as customer credit, or apply it "
                    f"to the {kind} that replaced it.",
                )
            raise SettlementTargetError(
                f"{kind.capitalize()} {number} is '{target.status}'; only a posted {kind} "
                f"can be settled.",
            )
        if target.customer_id != source.customer_id:
            owner = type(source.customer).objects.filter(
                pk=target.customer_id).values_list("code", flat=True).first()
            raise SettlementTargetError(
                f"{kind.capitalize()} {number} belongs to {owner}, not to "
                f"{source.customer.code}, whose {noun} this is. Apply it to one of "
                f"{source.customer.code}'s own documents, or raise a customer credit "
                f"transfer to move the credit to {owner}.",
            )
        if same_transaction_branch(source.entity.tenant_id, source.branch_id, target.branch_id):
            continue
        kind = "debit note" if isinstance(target, CreditNote) else "invoice"
        number = target.document_number or f"the selected {kind}"
        held = (f"belongs to {target.branch.name}" if target.branch_id
                else "has not been given a branch")
        if source.branch_id is None:
            raise SettlementBranchError(
                f"This {noun} has not been given a branch and {kind} {number} {held}, "
                f"so it cannot settle it."
            )
        name = source.branch.name
        article = "an" if name[:1].upper() in "AEIOU" else "a"
        raise SettlementBranchError(
            f"This {noun} belongs to {name} and {kind} {number} {held}. "
            f"Apply it to {article} {name} {kind}."
        )


# Support the apply payment subledger workflow.
def lock_settlement_targets(plan):
    """Re-read every target in *plan* under a row lock, returning the locked plan.

    Settlement is a read-modify-write: it reads ``target.balance_due`` (derived
    from the stored ``amount_paid``), decides how much to apply, and writes the
    sum back. ``@transaction.atomic`` makes that atomic; it does **not** make it
    isolated. On PostgreSQL's default READ COMMITTED, a second settlement running
    beside the first reads the *pre-update* row, so both compute their share
    against the same balance and the second write silently discards the first.

    What that costs is not a stale number. Each run also writes its own allocation
    row and posts its own journal crediting AR, so the sub-ledger records two
    settlements, the general ledger credits AR twice, and the document reports
    having been paid once. The AR control account is left carrying a credit that
    nothing in the books explains - which is exactly the state the "split at
    source" design in :func:`_post_payment_atomic` exists to make impossible.

    It is a real race, not a theoretical one: a bursar recording a counter receipt
    while the Paystack webhook for the same invoice is being confirmed is two
    concurrent settlements of one document, and both paths reach here.

    The lock is taken here rather than in each plan builder, and every settlement
    writer comes through it: receipts (:func:`_apply_payment_subledger`), credit
    notes (:func:`~vs_finance.credit_notes._apply_creditnote_subledger`),
    concessions and write-offs (which lock their one invoice the same way), and the
    payables mirrors. However its plan was built - the auto-allocation queryset, an
    explicit ``allocations`` list from a request body, the gateway booking path -
    the write reads a locked balance. The database backs this with check
    constraints, so a writer that misses the lock fails rather than settling a bill
    beyond its total.

    Ordering is the other half. Locks are taken model by model in a fixed order
    and by ascending primary key within each model, so two receipts that settle
    an overlapping set of documents always queue in the same sequence and cannot
    deadlock against each other.

    A target that has vanished between planning and locking is passed through
    untouched; the ``balance_due`` cap below then applies nothing for it, which is
    the same outcome as a document that was already settled.

    The caller's **own** instances are refreshed in place and handed back, rather
    than being replaced by the freshly fetched rows. That is not tidiness: the
    objects in ``plan`` belong to whoever built it, and several callers keep
    reading them afterwards. Swapping in copies would leave those callers holding
    a pre-settlement view of a document this function has just moved - which is
    precisely how ``void_expense_claim`` came to be asked whether a claim had been
    reimbursed and answer from a stale ``amount_paid``.
    """
    if not plan:
        return plan

    ids_by_model: dict[type, set] = {}
    by_key: dict[tuple, list] = {}
    for target, _amount in plan:
        pk = getattr(target, "pk", None)
        if pk is None:
            continue
        ids_by_model.setdefault(type(target), set()).add(pk)
        by_key.setdefault((type(target), pk), []).append(target)

    for model in sorted(ids_by_model, key=lambda m: m._meta.label):
        for row in (model.objects.select_for_update(of=("self",))
                    .filter(pk__in=sorted(ids_by_model[model]))
                    .order_by("pk")):
            for target in by_key.get((model, row.pk), ()):
                if target is row:
                    continue
                # Copy the locked row onto the caller's instance rather than
                # calling refresh_from_db() on each one, which would be a second
                # query per target for data this query has already returned.
                for field in row._meta.concrete_fields:
                    setattr(target, field.attname, getattr(row, field.attname))

    return plan


def _apply_payment_subledger(payment, plan, *, remaining):
    """Settle the plan's AR targets from a payment, capped at each target's balance and
    ``remaining``. A target is an :class:`Invoice` (→ PaymentAllocation, bump
    ``amount_paid``) or a DEBIT :class:`CreditNote` (→ DebitNoteAllocation, bump its
    ``amount_paid``). GL-agnostic - the caller posts the journal (the applied total
    credits AR either way).

    Returns ``(applied_total, created_rows, latest_target_date)``. The last value is the
    newest accounting date actually settled, which is what the caller needs to date its
    reclassification journal so AR is never credited before the receivable exists."""
    from .chronology import accounting_date
    from .models import CreditNote, DebitNoteAllocation, PaymentAllocation

    applied, created = 0, []  # Track total applied cash and created allocation rows.
    latest = None  # Newest accounting date this run actually settled.
    plan = lock_settlement_targets(plan)  # Nobody else may move these balances now.
    _require_settlable_targets(payment, [target for target, _amount in plan])
    for target, requested in plan:  # Walk the settlement plan in order.
        if remaining <= 0:  # Stop once all cash has been consumed.
            break  # Exit the current loop.
        apply_amount = min(int(requested), target.balance_due, remaining)  # Cap each allocation at all constraints.
        if apply_amount <= 0:  # Skip zero-value allocations.
            continue

        # One row per settlement event, never a running total. The caller stamps each
        # row with the date of the journal that credited AR for it, and a second
        # tranche against the same target credits AR on a different date - merging
        # them would leave one row that cannot honestly carry either date.
        if isinstance(target, CreditNote):  # Debit notes settle through their own allocation table.
            alloc = DebitNoteAllocation.objects.create(
                payment=payment, note=target, amount=apply_amount,
            )

            target.amount_paid += apply_amount  # Increase the debit note's paid amount.
            target.refresh_settlement_status(save=False)  # Recompute the debit note settlement state.
            target.save(update_fields=["amount_paid", "settlement_status", "updated_at"])
        else:  # Invoices use the normal payment allocation table.
            alloc = PaymentAllocation.objects.create(
                payment=payment, invoice=target, amount=apply_amount,
            )

            target.amount_paid += apply_amount  # Increase the invoice's paid amount.
            target.refresh_payment_status(save=False)  # Recompute the invoice payment status.
            target.save(update_fields=["amount_paid", "payment_status", "updated_at"])
            # Keep any installment plan on this invoice in step with the new settlement.  # Sync installment state.
            from .installments import refresh_plans_for_invoice
            refresh_plans_for_invoice(target)  # Refresh linked installment plans.

        remaining -= apply_amount  # Reduce the remaining unapplied cash.
        applied += apply_amount  # Track the total applied amount.
        created.append(alloc)  # Collect the allocation rows created by this run.
        target_date = accounting_date(target)  # Date of the document just settled.
        if target_date is not None and (latest is None or target_date > latest):
            latest = target_date  # Track the newest settled document date.
    return applied, created, latest  # Settled amount, allocation rows, newest settled date.


# Stamp a run's allocation rows with the date its journal credited AR.
def stamp_allocation_effective_date(rows, effective):
    """Record ``effective`` on every allocation row a settlement run just wrote.

    The sub-ledger and the GL have to agree at every point on the timeline, so an
    allocation's effective date is not "when someone clicked" and not the crediting
    document's own date - it is the date of the journal that actually credited AR for
    it. One run raises one journal, so every row it wrote shares that date, and an
    "as at" reconstruction that sums rows effective on or before a cutoff lands on
    exactly the figure the ledger shows.
    """
    if effective is None or not rows:  # Nothing to stamp.
        return
    for row in rows:
        row.effective_date = effective
    # One statement for the whole run: a large receipt can settle many invoices, and
    # a save per row would put that many round-trips inside the posting transaction.
    type(rows[0]).objects.bulk_update(rows, ["effective_date"], batch_size=500)


@transaction.atomic
# Support the post payment atomic workflow.
def _post_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                         strategy="oldest"):
    """Post a draft receipt: settle invoices, book the cash, and cut the GL journal.

    Runs in one transaction so the subledger (PaymentAllocation rows + invoice
    ``amount_paid``) and the general ledger (JournalEntry/JournalLine) can never
    drift apart - either everything commits or nothing does.

    Steps:
      1. **Guard.** Only a DRAFT payment with a positive amount can post, and the
         customer must have an AR control account and the payment a deposit
         (bank/cash) account - otherwise there's nowhere to book the two sides.
      2. **Plan.** ``_build_invoice_plan`` turns ``allocations`` (an explicit
         ``[(invoice, amount)]`` list) or ``auto_allocate`` (open invoices in
         ``strategy`` order - ``"oldest"`` by due date, ``"largest"`` by balance)
         into what to settle. Empty plan if neither is supplied.
      3. **Apply to subledger.** ``_apply_payment_subledger`` writes/extends the
         PaymentAllocation rows and bumps each invoice's ``amount_paid`` (capped at
         its balance and the cash left), returning ``applied`` - the total actually
         settled. It touches no GL accounts; this function owns the journal.
      4. **Split the cash.** ``excess = amount - applied`` is unapplied cash. We
         *split at source*: settled cash clears AR, but any excess is booked as a
         customer-credit liability so the AR control account never carries a credit
         balance. (That stored credit is later drained by ``allocate_payment``.)
      5. **Journal.** Balanced entry - Dr deposit account (cash in) for the full
         amount; Cr AR for ``applied``; Cr customer-credit (2140) for ``excess``.
         ``post_journal`` validates it balances and marks it posted.
      6. **Finalise.** Link the journal, flip status to POSTED, store
         ``allocated_amount``, and write a PAYMENT_POSTED audit record.

    Returns the updated ``payment``. Raises ``PostingError`` on any guard failure.
    """
    from .models import JournalEntry, JournalLine

    if payment.status != DocumentStatus.DRAFT:  # Only draft receipts can be posted.
        raise PostingError(
            f"Payment {payment.document_number or payment.pk} is '{payment.status}', "
            f"only a draft payment can be posted.",
        )
    if payment.amount <= 0:  # Reject zero or negative receipts.
        raise PostingError("A payment must have a positive amount to post.")

    customer = payment.customer  # The customer determines the AR control account.
    ar_account = customer.receivable_account  # Resolve the AR control account once.
    if ar_account is None:  # Posting requires an AR control account.
        raise PostingError(f"Customer {customer.code} has no receivable (AR control) account set.")
    if payment.deposit_account_id is None:  # Cash must post into a bank/cash account.
        raise PostingError("Payment has no deposit (bank/cash) account set.")

    # Split at source: settle open AR items (invoices + debit notes) against AR, and
    # book any unapplied cash as a customer-credit liability (so AR never carries a
    # credit balance).
    # ``as_of`` the receipt's own date: cash received today cannot clear a bill raised
    # next week. Auto-allocation skips those, and the money falls through to 2140 as a
    # prepayment - which is what it is.
    plan = (_build_invoice_plan(payment, allocations, strategy=strategy,  # Build the settlement plan from invoices.
                                include_debit_notes=True, as_of=payment.payment_date,
                                settlement=f"Receipt {payment.document_number or payment.pk}")
            if (allocations is not None or auto_allocate) else [])  # Skip the plan when no allocation is requested.
    applied, created_rows, _latest = _apply_payment_subledger(payment, plan, remaining=payment.amount)  # Apply the plan to AR.
    excess = payment.amount - applied  # Any leftover cash becomes customer credit.
    # The receipt's own journal (raised below, dated payment_date) is what credits AR
    # for everything this run settled, so that is these rows' effective date. The plan
    # was filtered to targets already raised by then, so no target post-dates it.
    stamp_allocation_effective_date(created_rows, payment.payment_date)

    period = resolve_period(payment.entity, payment.payment_date)  # Find the open accounting period.
    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Receipt {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    line_no = 0  # Track journal line ordering.
    line_no += 1  # First line is the cash/deposit debit.
    JournalLine.objects.create(
        entry=entry, account=payment.deposit_account, debit=payment.amount, credit=0,
        description=f"Receipt: {customer.code}", line_no=line_no,
    )
    if applied > 0:  # Only book AR if the payment settled at least one document.
        line_no += 1  # Advance to the AR credit line.
        JournalLine.objects.create(
            entry=entry, account=ar_account, debit=0, credit=applied,
            description=f"AR: {customer.code}", line_no=line_no,
        )
    if excess > 0:  # Unapplied cash becomes customer credit liability.
        line_no += 1  # Advance to the customer-credit line.
        JournalLine.objects.create(
            entry=entry, account=resolve_mapped_account(payment.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),
            debit=0, credit=excess, description=f"Customer credit: {customer.code}", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)  # Validate and mark the journal posted.

    payment.journal = entry  # Link the payment to the posted journal.
    payment.status = DocumentStatus.POSTED  # Mark the receipt as posted.
    payment.allocated_amount = applied  # Store the amount actually applied to documents.
    payment.save(update_fields=["journal", "status", "allocated_amount", "updated_at"])

    record(  # Log the successful payment posting in the audit trail.
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=f"Posted receipt from {customer.code} ({payment.amount} kobo).",
        journal_id=entry.pk, amount=payment.amount,
        allocated=applied, unallocated=excess,
    )
    return payment  # Return the posted payment.


@transaction.atomic
def allocate_payment(payment, *, allocations=None, actor_user=None, strategy="oldest"):
    """Apply a posted payment's **stored customer credit** to invoices.

    After posting, any unapplied cash sits in the customer-credit liability (2140).
    Applying it to invoices reclassifies it back to AR (``Dr customer-credit · Cr AR``)
    and settles the invoices - no cash moves. ``allocations`` is an optional explicit
    ``[(invoice, amount)]`` plan; without it, open invoices are settled in ``strategy``
    order (``"oldest"`` by due date, or ``"largest"`` balance first). Either way only
    documents of the receipt's own branch are settled (see :func:`_build_invoice_plan`).

    Applying an older receipt to a newer invoice is ordinary and allowed - that is a
    prepayment finding its bill. What is *not* allowed is dating the reclassification
    on the receipt's date when the invoice is newer, which would credit AR before the
    receivable existed; the journal is dated at the later of the two instead.
    """
    from .chronology import effective_allocation_date
    from .models import Customer, CustomerCreditAllocationJournal, JournalEntry, JournalLine

    payment = type(payment).objects.select_for_update().get(pk=payment.pk)
    customer = Customer.objects.select_for_update().get(pk=payment.customer_id)

    if payment.status != DocumentStatus.POSTED:  # Only posted receipts can be allocated later.
        raise PostingError("Only a posted payment can be allocated.")

    # Credit *remaining*, not merely unallocated: cash already refunded back out has
    # left 2140 and cannot be reclassified to AR a second time.
    remaining = payment.credit_remaining  # Stored customer credit still available on this receipt.
    if remaining <= 0:  # Nothing left to allocate.
        return []

    plan = _build_invoice_plan(payment, allocations, strategy=strategy,  # Reuse the same allocation planner.
                               include_debit_notes=True)
    applied, created, latest = _apply_payment_subledger(payment, plan, remaining=remaining)  # Apply stored credit to documents.
    if applied <= 0:  # No documents were eligible for allocation.
        return []

    effective = effective_allocation_date(payment.payment_date, [latest])  # Later of receipt and settled docs.
    stamp_allocation_effective_date(created, effective)  # Rows carry the date this run credits AR.
    period = resolve_period(payment.entity, effective)  # Find the open accounting period.
    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=effective, period=period,
        source=JournalSource.SALES, currency=payment.currency,
        narration=f"Apply customer credit: {customer.code}",
        reference=payment.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_mapped_account(payment.entity, AccountMappingKey.CUSTOMER_CREDIT, label="customer credit"),
        debit=applied, credit=0, description=f"Customer credit applied: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=customer.receivable_account, debit=0, credit=applied,
        description=f"AR: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)  # Validate and post the reclassification journal.

    CustomerCreditAllocationJournal.objects.create(
        payment=payment, journal=entry, amount=applied,
    )

    payment.allocated_amount += applied  # Increase the receipt's applied total.
    payment.save(update_fields=["allocated_amount", "updated_at"])

    record(  # Log the allocation in the finance audit trail.
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_ALLOCATED,
        actor_user=actor_user, target=payment,
        message=f"Applied {applied} kobo customer credit across {len(created)} invoice(s).",
        journal_id=entry.pk, allocated=payment.allocated_amount,
        unallocated=payment.credit_remaining, effective_date=str(effective),
    )
    return created  # Return the allocation rows that were created or extended.
