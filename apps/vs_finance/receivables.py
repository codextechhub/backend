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

from .accounts import resolve_account
from .audit import record, record_rejection
from .constants import (
    CUSTOMER_CREDIT_CODE,
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
    # Cr revenue, grouped by (account, cost centre) so the journal stays tidy while the
    # cost-centre split survives into the GL. Revenue is P&L, so it carries the analytics;
    # the AR control (above) and the output-tax liability (below) do not.
    revenue_by_key: dict[tuple[int, int | None], int] = defaultdict(int)
    revenue_objs: dict[tuple[int, int | None], tuple] = {}
    tax_by_account: dict[int, int] = defaultdict(int)
    tax_objs: dict[int, object] = {}
    for line in invoice.lines.select_related(
        "revenue_account", "tax_code__collected_account", "cost_center",
    ):
        key = (line.revenue_account_id, line.cost_center_id)
        revenue_by_key[key] += line.net_amount
        revenue_objs[key] = (line.revenue_account, line.cost_center)
        if line.tax_amount:
            tax_acc = line.tax_code.collected_account if line.tax_code_id else None
            if tax_acc is None:
                raise PostingError(
                    f"Tax code '{line.tax_code.code}' has no collected (output) account set."
                    if line.tax_code_id else "Tax amount present without a tax code.",
                )
            tax_by_account[tax_acc.id] += line.tax_amount
            tax_objs[tax_acc.id] = tax_acc

    for (acc_id, cc_id), amount in revenue_by_key.items():
        if amount == 0:
            continue
        line_no += 1
        revenue_account, cost_center = revenue_objs[(acc_id, cc_id)]
        JournalLine.objects.create(
            entry=entry, account=revenue_account, debit=0, credit=amount,
            description="Revenue", cost_center=cost_center, line_no=line_no,
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


def post_opening_balance(customer, *, actor_user=None, date=None):
    """Seat a customer's ``opening_balance`` as a posted opening invoice.

    Raises an :class:`~vs_finance.models.Invoice` (``source=OPENING``) that posts
    ``Dr AR control · Cr Retained Earnings`` — the opening figure is prior-period
    value, so it credits **equity**, never current-period revenue (crediting
    revenue would overstate the income statement every time a customer is migrated
    in with a balance). It still shows in the customer's outstanding (which is
    invoice-derived) *and* in the GL. No-op unless the opening balance is positive.
    Returns the invoice or ``None``. Runs the normal :func:`post_invoice` guards
    (open period, etc.).
    """
    import datetime

    from .constants import InvoiceSource, RETAINED_EARNINGS_CODE
    from .models import Invoice, InvoiceLine

    amount = int(customer.opening_balance or 0)
    if amount <= 0:
        return None

    # Opening balances are prior-period value: credit equity (Retained Earnings),
    # not revenue — otherwise onboarding/migrating a customer inflates this year's P&L.
    opening_equity = resolve_account(
        customer.entity, RETAINED_EARNINGS_CODE, label="opening balance equity",
    )
    invoice = Invoice.objects.create(
        entity=customer.entity, customer=customer,
        invoice_date=date or datetime.date.today(),
        source=InvoiceSource.OPENING,
        narration=f"Opening balance for {customer.code}",
        created_by=actor_user,
    )
    InvoiceLine.objects.create(
        invoice=invoice, revenue_account=opening_equity,
        quantity=1, unit_price=amount, line_no=1,
    )
    post_invoice(invoice, actor_user=actor_user)
    return invoice


# --------------------------------------------------------------------------- #
# Payment posting + allocation                                                #
# --------------------------------------------------------------------------- #

def post_payment(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                 strategy="oldest"):
    """Post a customer :class:`Payment` (Dr bank, Cr AR) and allocate it to invoices.

    ``allocations`` (a list of ``(invoice, amount_kobo)``) applies an explicit split;
    otherwise ``auto_allocate`` settles open invoices in ``strategy`` order
    (``"oldest"`` by due date, or ``"largest"`` balance first).
    """
    try:
        return _post_payment_atomic(
            payment, actor_user=actor_user,
            auto_allocate=auto_allocate, allocations=allocations, strategy=strategy,
        )
    except FinanceError as exc:
        record_rejection(
            entity=payment.entity,
            action=FinanceAuditAction.PAYMENT_POSTED,
            exc=exc, actor_user=actor_user, target=payment,
        )
        raise


def customer_credit_balance(customer) -> int:
    """A customer's available credit in kobo (their position in the 2140 liability).

    Credit comes from unapplied receipts + unapplied CREDIT notes, less what has
    already been refunded back to them. This is what a refund may pay out.
    """
    from django.db.models import Sum

    from .constants import CreditNoteKind
    from .models import CreditNote, Payment, Refund

    pay = sum(p.unallocated_amount for p in Payment.objects.filter(
        customer=customer, status=DocumentStatus.POSTED))
    notes = sum(n.unallocated_amount for n in CreditNote.objects.filter(
        customer=customer, status=DocumentStatus.POSTED, kind=CreditNoteKind.CREDIT))
    refunded = Refund.objects.filter(
        customer=customer, status=DocumentStatus.POSTED).aggregate(s=Sum("amount"))["s"] or 0
    # A still-unsettled DEBIT note is an outstanding charge; it offsets refundable credit
    # so we never hand back cash that a supplementary charge still needs to collect.
    # Floored at zero: a net-negative position means the customer owes, not that they
    # have credit to refund.
    debit_due = sum(n.balance_due for n in CreditNote.objects.filter(
        customer=customer, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT))
    return max(0, pay + notes - refunded - debit_due)


#: Supported auto-allocation strategies for settling a receipt's cash.
ALLOCATION_STRATEGIES = ("oldest", "largest")


def _build_invoice_plan(customer, allocations, *, strategy="oldest", include_debit_notes=False):
    """An explicit ``[(target, amount)]`` plan, or open AR items in ``strategy`` order.

    A *target* is an :class:`Invoice` or — when ``include_debit_notes`` is set — a posted
    DEBIT :class:`CreditNote`, which debits AR just like an invoice and is settled the
    same way by receipts. Both expose ``balance_due``. ``strategy`` (when ``allocations``
    is not given): ``"oldest"`` settles by document date first (the default), ``"largest"``
    settles the biggest outstanding balance first. Debit-note settlement is opt-in because
    the credit-note sub-ledger can only point at invoices; payment paths pass it True.
    """
    from django.db.models import F

    from .constants import CreditNoteKind
    from .models import CreditNote, Invoice

    if allocations is not None:
        return list(allocations)

    open_invoices = list(
        Invoice.objects
        .filter(customer=customer, status=DocumentStatus.POSTED)
        .exclude(payment_status=InvoicePaymentStatus.PAID)
    )
    # (target, balance_due, sort_date) — sort_date drives oldest-first across both types.
    items = [(inv, inv.balance_due, inv.due_date or inv.invoice_date) for inv in open_invoices]
    if include_debit_notes:
        open_notes = list(
            CreditNote.objects
            .filter(customer=customer, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)
            .exclude(settlement_status=InvoicePaymentStatus.PAID)
        )
        items += [(dn, dn.balance_due, dn.note_date) for dn in open_notes]

    if strategy == "largest":
        items.sort(key=lambda t: (-t[1], t[2], t[0].pk))
    else:
        items.sort(key=lambda t: (t[2], t[0].pk))
    return [(target, balance) for target, balance, _date in items]


def _apply_payment_subledger(payment, plan, *, remaining):
    """Settle the plan's AR targets from a payment, capped at each target's balance and
    ``remaining``. A target is an :class:`Invoice` (→ PaymentAllocation, bump
    ``amount_paid``) or a DEBIT :class:`CreditNote` (→ DebitNoteAllocation, bump its
    ``amount_paid``). GL-agnostic — the caller posts the journal (the applied total
    credits AR either way). Returns ``(applied_total, created_rows)``."""
    from .models import CreditNote, DebitNoteAllocation, PaymentAllocation

    applied, created = 0, []
    for target, requested in plan:
        if remaining <= 0:
            break
        apply_amount = min(int(requested), target.balance_due, remaining)
        if apply_amount <= 0:
            continue

        if isinstance(target, CreditNote):
            alloc, _was = DebitNoteAllocation.objects.get_or_create(
                payment=payment, note=target, defaults={"amount": 0},
            )
            alloc.amount += apply_amount
            alloc.save(update_fields=["amount", "updated_at"])

            target.amount_paid += apply_amount
            target.refresh_settlement_status(save=False)
            target.save(update_fields=["amount_paid", "settlement_status", "updated_at"])
        else:
            alloc, _was = PaymentAllocation.objects.get_or_create(
                payment=payment, invoice=target, defaults={"amount": 0},
            )
            alloc.amount += apply_amount
            alloc.save(update_fields=["amount", "updated_at"])

            target.amount_paid += apply_amount
            target.refresh_payment_status(save=False)
            target.save(update_fields=["amount_paid", "payment_status", "updated_at"])
            # Keep any installment plan on this invoice in step with the new settlement.
            from .installments import refresh_plans_for_invoice
            refresh_plans_for_invoice(target)

        remaining -= apply_amount
        applied += apply_amount
        created.append(alloc)
    return applied, created


@transaction.atomic
def _post_payment_atomic(payment, *, actor_user=None, auto_allocate=True, allocations=None,
                         strategy="oldest"):
    """Post a draft receipt: settle invoices, book the cash, and cut the GL journal.

    Runs in one transaction so the subledger (PaymentAllocation rows + invoice
    ``amount_paid``) and the general ledger (JournalEntry/JournalLine) can never
    drift apart — either everything commits or nothing does.

    Steps:
      1. **Guard.** Only a DRAFT payment with a positive amount can post, and the
         customer must have an AR control account and the payment a deposit
         (bank/cash) account — otherwise there's nowhere to book the two sides.
      2. **Plan.** ``_build_invoice_plan`` turns ``allocations`` (an explicit
         ``[(invoice, amount)]`` list) or ``auto_allocate`` (open invoices in
         ``strategy`` order — ``"oldest"`` by due date, ``"largest"`` by balance)
         into what to settle. Empty plan if neither is supplied.
      3. **Apply to subledger.** ``_apply_payment_subledger`` writes/extends the
         PaymentAllocation rows and bumps each invoice's ``amount_paid`` (capped at
         its balance and the cash left), returning ``applied`` — the total actually
         settled. It touches no GL accounts; this function owns the journal.
      4. **Split the cash.** ``excess = amount - applied`` is unapplied cash. We
         *split at source*: settled cash clears AR, but any excess is booked as a
         customer-credit liability so the AR control account never carries a credit
         balance. (That stored credit is later drained by ``allocate_payment``.)
      5. **Journal.** Balanced entry — Dr deposit account (cash in) for the full
         amount; Cr AR for ``applied``; Cr customer-credit (2140) for ``excess``.
         ``post_journal`` validates it balances and marks it posted.
      6. **Finalise.** Link the journal, flip status to POSTED, store
         ``allocated_amount``, and write a PAYMENT_POSTED audit record.

    Returns the updated ``payment``. Raises ``PostingError`` on any guard failure.
    """
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

    # Split at source: settle open AR items (invoices + debit notes) against AR, and
    # book any unapplied cash as a customer-credit liability (so AR never carries a
    # credit balance).
    plan = (_build_invoice_plan(customer, allocations, strategy=strategy,
                                include_debit_notes=True)
            if (allocations is not None or auto_allocate) else [])
    applied, _created = _apply_payment_subledger(payment, plan, remaining=payment.amount)
    excess = payment.amount - applied

    period = resolve_period(payment.entity, payment.payment_date)
    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.BANK, currency=payment.currency,
        narration=payment.narration or f"Receipt {payment.document_number or ''}".strip(),
        reference=payment.reference, created_by=actor_user,
    )
    line_no = 0
    line_no += 1
    JournalLine.objects.create(
        entry=entry, account=payment.deposit_account, debit=payment.amount, credit=0,
        description=f"Receipt: {customer.code}", line_no=line_no,
    )
    if applied > 0:
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=ar_account, debit=0, credit=applied,
            description=f"AR: {customer.code}", line_no=line_no,
        )
    if excess > 0:
        line_no += 1
        JournalLine.objects.create(
            entry=entry, account=resolve_account(payment.entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
            debit=0, credit=excess, description=f"Customer credit: {customer.code}", line_no=line_no,
        )

    post_journal(entry, actor_user=actor_user)

    payment.journal = entry
    payment.status = DocumentStatus.POSTED
    payment.allocated_amount = applied
    payment.save(update_fields=["journal", "status", "allocated_amount", "updated_at"])

    record(
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_POSTED,
        actor_user=actor_user, target=payment,
        message=f"Posted receipt from {customer.code} ({payment.amount} kobo).",
        journal_id=entry.pk, amount=payment.amount,
        allocated=applied, unallocated=excess,
    )
    return payment


@transaction.atomic
def allocate_payment(payment, *, allocations=None, actor_user=None, strategy="oldest"):
    """Apply a posted payment's **stored customer credit** to invoices.

    After posting, any unapplied cash sits in the customer-credit liability (2140).
    Applying it to invoices reclassifies it back to AR (``Dr customer-credit · Cr AR``)
    and settles the invoices — no cash moves. ``allocations`` is an optional explicit
    ``[(invoice, amount)]`` plan; without it, open invoices are settled in ``strategy``
    order (``"oldest"`` by due date, or ``"largest"`` balance first).
    """
    from .models import JournalEntry, JournalLine

    if payment.status != DocumentStatus.POSTED:
        raise PostingError("Only a posted payment can be allocated.")

    remaining = payment.unallocated_amount
    if remaining <= 0:
        return []

    plan = _build_invoice_plan(payment.customer, allocations, strategy=strategy,
                               include_debit_notes=True)
    applied, created = _apply_payment_subledger(payment, plan, remaining=remaining)
    if applied <= 0:
        return []

    customer = payment.customer
    period = resolve_period(payment.entity, payment.payment_date)
    entry = JournalEntry.objects.create(
        entity=payment.entity, branch=payment.branch,
        date=payment.payment_date, period=period,
        source=JournalSource.SALES, currency=payment.currency,
        narration=f"Apply customer credit: {customer.code}",
        reference=payment.reference, created_by=actor_user,
    )
    JournalLine.objects.create(
        entry=entry, account=resolve_account(payment.entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
        debit=applied, credit=0, description=f"Customer credit applied: {customer.code}", line_no=1,
    )
    JournalLine.objects.create(
        entry=entry, account=customer.receivable_account, debit=0, credit=applied,
        description=f"AR: {customer.code}", line_no=2,
    )
    post_journal(entry, actor_user=actor_user)

    payment.allocated_amount += applied
    payment.save(update_fields=["allocated_amount", "updated_at"])

    record(
        entity=payment.entity, action=FinanceAuditAction.PAYMENT_ALLOCATED,
        actor_user=actor_user, target=payment,
        message=f"Applied {applied} kobo customer credit across {len(created)} invoice(s).",
        journal_id=entry.pk, allocated=payment.allocated_amount, unallocated=payment.unallocated_amount,
    )
    return created
