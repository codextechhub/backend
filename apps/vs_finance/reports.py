"""Read-side reporting over the ledger.

These functions only *read* the denormalised :class:`~vs_finance.models.AccountBalance`
aggregates that :mod:`vs_finance.posting` maintains, so they are cheap and never
re-sum the whole journal. The cardinal invariant they exist to demonstrate: a
double-entry ledger's debits and credits are always equal, so a trial balance over a
balanced set of postings **always balances**.
"""
from __future__ import annotations  # Import dependency used by this finance module.

from dataclasses import dataclass, field  # Import dependency used by this finance module.

from django.utils import timezone  # Import dependency used by this finance module.

from .money import format_naira  # Import dependency used by this finance module.


@dataclass  # Decorator configures the following callable.
class TrialBalanceRow:  # Class groups related finance API or service behavior.
    """One account's debit/credit position on the trial balance (kobo)."""

    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    debit: int  # Finance processing step.
    credit: int  # Finance processing step.

    @property  # Decorator configures the following callable.
    def debit_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.debit)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def credit_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.credit)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class TrialBalance:  # Class groups related finance API or service behavior.
    """A trial balance for an entity (optionally a single period).

    ``is_balanced`` is the headline check; in a correct ledger it is always ``True``.
    """

    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    rows: list[TrialBalanceRow] = field(default_factory=list)  # Store intermediate finance value.
    total_debit: int = 0  # Store intermediate finance value.
    total_credit: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def is_balanced(self) -> bool:  # Function handles this finance operation.
        return self.total_debit == self.total_credit  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def difference(self) -> int:  # Function handles this finance operation.
        return self.total_debit - self.total_credit  # Return the computed finance response.


def trial_balance(entity, *, period=None) -> TrialBalance:  # Function handles this finance operation.
    """Build a trial balance for ``entity``, optionally scoped to one ``period``.

    Each account's net position is reduced to a single side: if accumulated debits
    exceed credits the remainder sits in the debit column, else the credit column —
    the conventional trial-balance presentation. Because every posted journal
    balanced, the column totals are equal.
    """
    from .models import AccountBalance  # Import dependency used by this finance module.

    qs = AccountBalance.objects.filter(account__entity=entity).select_related("account")  # Query finance data from the database.
    if period is not None:  # Branch when this finance condition is true.
        # Cumulative balance AS OF the selected period — every movement up to and
        # including it. A trial balance is a point-in-time statement of balances,
        # not one period's activity, so "Jun 2026" means the running balance through
        # June, not June's movement alone. (Openings aren't rolled forward in this
        # ledger — each row carries only its period's movement — so a straight sum
        # of movements through the period is the running balance.)
        qs = qs.filter(period__start_date__lte=period.start_date)  # Store intermediate finance value.

    # Sum each account's movement across the in-scope periods.
    by_account: dict[int, dict] = {}  # Store intermediate finance value.
    for bal in qs:  # Iterate through finance records.
        acc = bal.account  # Store intermediate finance value.
        slot = by_account.setdefault(  # Store intermediate finance value.
            acc.id,  # Finance processing step.
            {  # Continue structured finance payload.
                "code": acc.code, "name": acc.name, "account_type": acc.account_type,  # Finance processing step.
                "debit": 0, "credit": 0,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        slot["debit"] += (bal.opening_debit + bal.debit_total)  # Store intermediate finance value.
        slot["credit"] += (bal.opening_credit + bal.credit_total)  # Store intermediate finance value.

    rows: list[TrialBalanceRow] = []  # Store intermediate finance value.
    total_debit = 0  # Store intermediate finance value.
    total_credit = 0  # Store intermediate finance value.
    for account_id, slot in sorted(by_account.items(), key=lambda kv: kv[1]["code"]):  # Iterate through finance records.
        net = slot["debit"] - slot["credit"]  # Store intermediate finance value.
        debit = net if net > 0 else 0  # Store intermediate finance value.
        credit = -net if net < 0 else 0  # Store intermediate finance value.
        if debit == 0 and credit == 0:  # Branch when this finance condition is true.
            continue  # net-zero accounts don't clutter the statement
        total_debit += debit  # Store intermediate finance value.
        total_credit += credit  # Store intermediate finance value.
        rows.append(  # Finance processing step.
            TrialBalanceRow(  # Finance processing step.
                account_id=account_id,  # Store intermediate finance value.
                code=slot["code"], name=slot["name"], account_type=slot["account_type"],  # Store intermediate finance value.
                debit=debit, credit=credit,  # Store intermediate finance value.
            )  # Continue structured finance payload.
        )  # Continue structured finance payload.

    return TrialBalance(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        rows=rows,  # Store intermediate finance value.
        total_debit=total_debit,  # Store intermediate finance value.
        total_credit=total_credit,  # Store intermediate finance value.
    )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Analytical slice — net activity per account, bucketed by an axis            #
# --------------------------------------------------------------------------- #

@dataclass  # Decorator configures the following callable.
class AnalyticsSliceRow:  # Class groups related finance API or service behavior.
    """One account's net movement within a single analytical bucket (kobo)."""

    bucket: str  # Finance processing step.
    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    debit: int  # Finance processing step.
    credit: int  # Finance processing step.

    @property  # Decorator configures the following callable.
    def net(self) -> int:  # Function handles this finance operation.
        return self.debit - self.credit  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def net_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.net)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class AnalyticsSlice:  # Class groups related finance API or service behavior.
    """Posted activity for an entity sliced by one axis (a cost centre or a dimension).

    Unlike the trial balance this reads posted :class:`~vs_finance.models.JournalLine`
    rows directly — the denormalised ``AccountBalance`` carries neither the cost centre
    nor the dimensions map, so it cannot answer "per bucket" questions.
    """

    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    axis: str  # Finance processing step.
    rows: list[AnalyticsSliceRow] = field(default_factory=list)  # Store intermediate finance value.
    bucket_totals: dict[str, int] = field(default_factory=dict)  # Store intermediate finance value.
    total_net: int = 0  # Store intermediate finance value.


def analytics_slice(entity, *, axis, period=None, account_type=None) -> AnalyticsSlice:  # Function handles this finance operation.
    """Net movement per account, bucketed by ``axis``, over posted journals.

    ``axis`` is either the literal ``"cost_center"`` or a :class:`~vs_finance.models.Dimension`
    code (e.g. ``"FUND"``). **Only lines actually tagged on the axis are included** — a
    line with no cost centre (or no value for the dimension) is not part of that axis's
    analysis and is skipped, so the report shows genuinely-allocated activity rather
    than a catch-all bucket. Optionally scope to one ``period`` and/or one
    ``account_type``. Net is ``debit - credit`` (kobo) so it reads naturally for both
    sides of the books.
    """
    from .constants import DocumentStatus  # Import dependency used by this finance module.
    from .models import JournalLine  # Import dependency used by this finance module.

    qs = (  # Store intermediate finance value.
        JournalLine.objects  # Query finance data from the database.
        .filter(entry__entity=entity, entry__status=DocumentStatus.POSTED)  # Store intermediate finance value.
        .select_related("account", "cost_center")  # Finance processing step.
    )  # Continue structured finance payload.
    if axis == "cost_center":  # Branch when this finance condition is true.
        qs = qs.filter(cost_center__isnull=False)  # only cost-centre-tagged lines
    if period is not None:  # Branch when this finance condition is true.
        qs = qs.filter(entry__period=period)  # Store intermediate finance value.
    if account_type:  # Branch when this finance condition is true.
        qs = qs.filter(account__account_type=account_type)  # Store intermediate finance value.

    by_key: dict[tuple, dict] = {}  # Store intermediate finance value.
    bucket_totals: dict[str, int] = {}  # Store intermediate finance value.
    total_net = 0  # Store intermediate finance value.
    for line in qs:  # Iterate through finance records.
        if axis == "cost_center":  # Branch when this finance condition is true.
            bucket = line.cost_center.code  # Store intermediate finance value.
        else:  # Fallback finance branch.
            bucket = (line.dimensions or {}).get(axis)  # Store intermediate finance value.
            if not bucket:  # Branch when this finance condition is true.
                continue  # untagged on this dimension → not part of the analysis
        acc = line.account  # Store intermediate finance value.
        slot = by_key.setdefault(  # Store intermediate finance value.
            (bucket, acc.id),  # Continue structured finance payload.
            {  # Continue structured finance payload.
                "bucket": bucket, "code": acc.code, "name": acc.name,  # Finance processing step.
                "account_type": acc.account_type, "debit": 0, "credit": 0,  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.
        slot["debit"] += line.debit  # Store intermediate finance value.
        slot["credit"] += line.credit  # Store intermediate finance value.

    rows: list[AnalyticsSliceRow] = []  # Store intermediate finance value.
    for (bucket, account_id), slot in sorted(  # Iterate through finance records.
        by_key.items(), key=lambda kv: (kv[0][0], kv[1]["code"])  # Store intermediate finance value.
    ):  # Continue structured finance payload.
        net = slot["debit"] - slot["credit"]  # Store intermediate finance value.
        if net == 0:  # Branch when this finance condition is true.
            continue  # net-zero account/bucket pairs don't clutter the slice
        rows.append(  # Finance processing step.
            AnalyticsSliceRow(  # Finance processing step.
                bucket=bucket, account_id=account_id,  # Store intermediate finance value.
                code=slot["code"], name=slot["name"], account_type=slot["account_type"],  # Store intermediate finance value.
                debit=slot["debit"], credit=slot["credit"],  # Store intermediate finance value.
            )  # Continue structured finance payload.
        )  # Continue structured finance payload.
        bucket_totals[bucket] = bucket_totals.get(bucket, 0) + net  # Store intermediate finance value.
        total_net += net  # Store intermediate finance value.

    return AnalyticsSlice(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        axis=axis,  # Store intermediate finance value.
        rows=rows,  # Store intermediate finance value.
        bucket_totals=bucket_totals,  # Store intermediate finance value.
        total_net=total_net,  # Store intermediate finance value.
    )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Accounts-Receivable aging + control reconciliation                          #
# --------------------------------------------------------------------------- #

#: Aging bucket labels, in order. "current" = not yet overdue.
AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")  # Store intermediate finance value.


def _bucket_for(days_overdue: int) -> str:  # Function handles this finance operation.
    if days_overdue <= 0:  # Branch when this finance condition is true.
        return "current"  # Return the computed finance response.
    if days_overdue <= 30:  # Branch when this finance condition is true.
        return "1-30"  # Return the computed finance response.
    if days_overdue <= 60:  # Branch when this finance condition is true.
        return "31-60"  # Return the computed finance response.
    if days_overdue <= 90:  # Branch when this finance condition is true.
        return "61-90"  # Return the computed finance response.
    return "90+"  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class AgingRow:  # Class groups related finance API or service behavior.
    """One customer's outstanding AR, split into aging buckets (kobo)."""

    customer_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    buckets: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})  # Store intermediate finance value.
    outstanding: int = 0          # gross of unapplied credit
    unallocated_credit: int = 0   # open payment credit not yet applied
    net: int = 0                  # outstanding - unallocated_credit


@dataclass  # Decorator configures the following callable.
class AgingReport:  # Class groups related finance API or service behavior.
    entity_id: int  # Finance processing step.
    as_of: object  # Finance processing step.
    rows: list = field(default_factory=list)  # Store intermediate finance value.
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})  # Store intermediate finance value.
    total_outstanding: int = 0  # Store intermediate finance value.
    total_unallocated_credit: int = 0  # Store intermediate finance value.
    total_net: int = 0  # Store intermediate finance value.


def ar_aging(entity, *, as_of=None) -> AgingReport:  # Function handles this finance operation.
    """Age each customer's open invoices into current/1-30/31-60/61-90/90+ buckets.

    An invoice ages off its ``due_date`` (falling back to ``invoice_date``). Only
    POSTED, not-fully-paid invoices contribute, by their ``balance_due``. Each
    customer's unallocated payment credit is reported and netted, so ``total_net``
    equals the AR control account's GL balance (see :func:`reconcile_ar`).
    """
    from .models import Invoice, Payment  # Import dependency used by this finance module.

    as_of = as_of or timezone.now().date()  # Store intermediate finance value.
    report = AgingReport(entity_id=entity.id, as_of=as_of)  # Store intermediate finance value.
    rows: dict[int, AgingRow] = {}  # Store intermediate finance value.

    def row_for(customer):  # Function handles this finance operation.
        r = rows.get(customer.id)  # Store intermediate finance value.
        if r is None:  # Branch when this finance condition is true.
            r = AgingRow(  # Store intermediate finance value.
                customer_id=customer.id, code=customer.code, name=customer.name,  # Store intermediate finance value.
                buckets={b: 0 for b in AGING_BUCKETS},  # Store intermediate finance value.
            )  # Continue structured finance payload.
            rows[customer.id] = r  # Store intermediate finance value.
        return r  # Return the computed finance response.

    posted_invoices = (  # Store intermediate finance value.
        Invoice.objects  # Query finance data from the database.
        .filter(entity=entity, status="POSTED")  # Store intermediate finance value.
        .exclude(payment_status="PAID")  # Store intermediate finance value.
        .select_related("customer")  # Finance processing step.
    )  # Continue structured finance payload.
    for inv in posted_invoices:  # Iterate through finance records.
        due = inv.balance_due  # Store intermediate finance value.
        if due <= 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        ref_date = inv.due_date or inv.invoice_date  # Store intermediate finance value.
        days_overdue = (as_of - ref_date).days  # Store intermediate finance value.
        bucket = _bucket_for(days_overdue)  # Store intermediate finance value.
        r = row_for(inv.customer)  # Store intermediate finance value.
        r.buckets[bucket] += due  # Store intermediate finance value.
        r.outstanding += due  # Store intermediate finance value.

    # Unallocated payment credit reduces a customer's net balance.
    posted_payments = (  # Store intermediate finance value.
        Payment.objects.filter(entity=entity, status="POSTED").select_related("customer")  # Query finance data from the database.
    )  # Continue structured finance payload.
    for pay in posted_payments:  # Iterate through finance records.
        credit = pay.unallocated_amount  # Store intermediate finance value.
        if credit <= 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        r = row_for(pay.customer)  # Store intermediate finance value.
        r.unallocated_credit += credit  # Store intermediate finance value.

    for r in rows.values():  # Iterate through finance records.
        r.net = r.outstanding - r.unallocated_credit  # Store intermediate finance value.
        for b in AGING_BUCKETS:  # Iterate through finance records.
            report.bucket_totals[b] += r.buckets[b]  # Store intermediate finance value.
        report.total_outstanding += r.outstanding  # Store intermediate finance value.
        report.total_unallocated_credit += r.unallocated_credit  # Store intermediate finance value.
        report.total_net += r.net  # Store intermediate finance value.

    report.rows = sorted(rows.values(), key=lambda x: x.code)  # Store intermediate finance value.
    return report  # Return the computed finance response.


def _account_gl_net(account) -> int:  # Function handles this finance operation.
    """Net GL movement for an account across all its periods, signed to normal balance."""
    from .constants import NormalBalance  # Import dependency used by this finance module.

    total = 0  # Store intermediate finance value.
    for bal in account.balances.all():  # Iterate through finance records.
        dr = bal.opening_debit + bal.debit_total  # Store intermediate finance value.
        cr = bal.opening_credit + bal.credit_total  # Store intermediate finance value.
        total += (dr - cr) if account.normal_balance == NormalBalance.DEBIT else (cr - dr)  # Store intermediate finance value.
    return total  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class ARReconciliation:  # Class groups related finance API or service behavior.
    entity_id: int  # Finance processing step.
    subledger_total: int     # from the AR aging (customer balances)
    control_total: int       # from the AR control account(s) in the GL
    difference: int  # Finance processing step.

    @property  # Decorator configures the following callable.
    def is_reconciled(self) -> bool:  # Function handles this finance operation.
        return self.difference == 0  # Return the computed finance response.


def reconcile_ar(entity, *, as_of=None) -> ARReconciliation:  # Function handles this finance operation.
    """Assert the AR **sub-ledger** (customer balances) equals the AR **control** GL.

    The cardinal AR control: the sum of what every customer owes must equal the
    balance of the receivable control account(s) in the ledger. Any drift means a
    posting bypassed the sub-ledger (or vice-versa) and must be investigated.
    """
    from .models import Customer  # Import dependency used by this finance module.

    aging = ar_aging(entity, as_of=as_of)  # Store intermediate finance value.
    subledger_total = aging.total_net  # Store intermediate finance value.

    control_accounts = {  # Store intermediate finance value.
        c.receivable_account  # Finance processing step.
        for c in Customer.objects.filter(entity=entity).select_related("receivable_account")  # Iterate through finance records.
        if c.receivable_account_id is not None  # Branch when this finance condition is true.
    }  # Continue structured finance payload.
    control_total = sum(_account_gl_net(acc) for acc in control_accounts)  # Store intermediate finance value.

    return ARReconciliation(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        subledger_total=subledger_total,  # Store intermediate finance value.
        control_total=control_total,  # Store intermediate finance value.
        difference=subledger_total - control_total,  # Store intermediate finance value.
    )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Customer statement of account                                               #
# --------------------------------------------------------------------------- #


@dataclass  # Decorator configures the following callable.
class StatementEntry:  # Class groups related finance API or service behavior.
    """One movement on a customer's account — a debit *raises* what they owe.

    Invoices, debit notes and refunds debit (increase the receivable); receipts,
    credit notes and concessions credit (reduce it). ``balance`` is the running
    receivable after this entry, in kobo.
    """

    date: object  # Finance processing step.
    sort_key: tuple  # Finance processing step.
    doc_type: str  # Finance processing step.
    document_number: str  # Finance processing step.
    description: str  # Finance processing step.
    debit: int = 0  # Store intermediate finance value.
    credit: int = 0  # Store intermediate finance value.
    balance: int = 0  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class CustomerStatement:  # Class groups related finance API or service behavior.
    """A dated ledger of a customer's account with a running balance (all kobo).

    Built from the customer's *posted* AR documents — invoices, receipts,
    credit/debit notes, refunds and concessions. ``opening_balance`` is the net of
    everything dated before ``start_date``; ``entries`` are the movements within
    ``[start_date, end_date]``; ``closing_balance`` is where the account stands at
    ``end_date``. ``aging`` buckets the customer's still-open invoice balances as at
    ``end_date``. (Bad-debt write-offs clear an invoice internally and are not itemised
    as their own statement line; the aging block always reflects live balances.)
    """

    entity_id: int  # Finance processing step.
    customer_id: int  # Finance processing step.
    customer_code: str  # Finance processing step.
    customer_name: str  # Finance processing step.
    start_date: object | None  # Finance processing step.
    end_date: object  # Finance processing step.
    opening_balance: int = 0  # Store intermediate finance value.
    entries: list = field(default_factory=list)  # Store intermediate finance value.
    total_debits: int = 0  # Store intermediate finance value.
    total_credits: int = 0  # Store intermediate finance value.
    closing_balance: int = 0  # Store intermediate finance value.
    aging: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})  # Store intermediate finance value.


def customer_statement(customer, *, start_date=None, end_date=None) -> CustomerStatement:  # Function handles this finance operation.
    """Build a :class:`CustomerStatement` for ``customer`` over ``[start_date, end_date]``.

    ``end_date`` defaults to today; ``start_date`` of ``None`` runs from the account's
    inception (a zero opening balance). Movements are ordered by date, then by a stable
    document-type ordering so same-day documents read sensibly (invoice before its
    receipt).
    """
    from .constants import CreditNoteKind, DocumentStatus  # Import dependency used by this finance module.
    from .models import Concession, CreditNote, Invoice, Payment, Refund  # Import dependency used by this finance module.

    entity = customer.entity  # Store intermediate finance value.
    end_date = end_date or timezone.now().date()  # Store intermediate finance value.

    # Each movement: (date, type_order, doc_type, number, description, debit, credit).
    movements: list = []  # Store intermediate finance value.

    for inv in Invoice.objects.filter(customer=customer, status=DocumentStatus.POSTED):  # Iterate through finance records.
        movements.append((  # Finance processing step.
            inv.invoice_date, 0, "Invoice", inv.document_number,  # Finance processing step.
            inv.narration or "Invoice", inv.total, 0,  # Finance processing step.
        ))  # Continue structured finance payload.
    for note in CreditNote.objects.filter(customer=customer, status=DocumentStatus.POSTED):  # Iterate through finance records.
        if note.kind == CreditNoteKind.DEBIT:  # Branch when this finance condition is true.
            movements.append((  # Finance processing step.
                note.note_date, 1, "Debit note", note.document_number,  # Finance processing step.
                note.reason or "Debit note", note.total, 0,  # Finance processing step.
            ))  # Continue structured finance payload.
        else:  # Fallback finance branch.
            movements.append((  # Finance processing step.
                note.note_date, 3, "Credit note", note.document_number,  # Finance processing step.
                note.reason or "Credit note", 0, note.total,  # Finance processing step.
            ))  # Continue structured finance payload.
    for refund in Refund.objects.filter(customer=customer, status=DocumentStatus.POSTED):  # Iterate through finance records.
        movements.append((  # Finance processing step.
            refund.refund_date, 2, "Refund", refund.document_number,  # Finance processing step.
            refund.narration or "Refund", refund.amount, 0,  # Finance processing step.
        ))  # Continue structured finance payload.
    for pay in Payment.objects.filter(customer=customer, status=DocumentStatus.POSTED):  # Iterate through finance records.
        movements.append((  # Finance processing step.
            pay.payment_date, 4, "Receipt", pay.document_number,  # Finance processing step.
            pay.narration or "Receipt", 0, pay.amount,  # Finance processing step.
        ))  # Continue structured finance payload.
    for con in Concession.objects.filter(customer=customer, status=DocumentStatus.POSTED):  # Iterate through finance records.
        movements.append((  # Finance processing step.
            con.concession_date, 5, con.get_kind_display(), con.document_number,  # Finance processing step.
            con.reason or con.get_kind_display(), 0, con.amount,  # Finance processing step.
        ))  # Continue structured finance payload.

    movements.sort(key=lambda m: (m[0], m[1], m[3]))  # Store intermediate finance value.

    statement = CustomerStatement(  # Store intermediate finance value.
        entity_id=entity.id, customer_id=customer.id,  # Store intermediate finance value.
        customer_code=customer.code, customer_name=customer.name,  # Store intermediate finance value.
        start_date=start_date, end_date=end_date,  # Store intermediate finance value.
    )  # Continue structured finance payload.

    balance = 0  # Store intermediate finance value.
    for date_, type_order, doc_type, number, description, debit, credit in movements:  # Iterate through finance records.
        if date_ > end_date:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        if start_date is not None and date_ < start_date:  # Branch when this finance condition is true.
            statement.opening_balance += debit - credit  # Store intermediate finance value.
            balance = statement.opening_balance  # Store intermediate finance value.
            continue  # Finance processing step.
        balance += debit - credit  # Store intermediate finance value.
        statement.entries.append(StatementEntry(  # Finance processing step.
            date=date_, sort_key=(date_, type_order), doc_type=doc_type,  # Store intermediate finance value.
            document_number=number, description=description,  # Store intermediate finance value.
            debit=debit, credit=credit, balance=balance,  # Store intermediate finance value.
        ))  # Continue structured finance payload.
        statement.total_debits += debit  # Store intermediate finance value.
        statement.total_credits += credit  # Store intermediate finance value.

    statement.closing_balance = (  # Store the computed statement closing balance.
        statement.opening_balance  # Start from the opening balance.
        + statement.total_debits  # Add period debit movement.
        - statement.total_credits  # Subtract period credit movement.
    )  # Close the closing-balance calculation.

    # Aging of the customer's still-open invoices as at end_date.
    for inv in (  # Iterate through finance records.
        Invoice.objects.filter(customer=customer, status=DocumentStatus.POSTED)  # Query finance data from the database.
        .exclude(payment_status="PAID")  # Store intermediate finance value.
    ):  # Continue structured finance payload.
        due = inv.balance_due  # Store intermediate finance value.
        if due <= 0 or inv.invoice_date > end_date:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        ref_date = inv.due_date or inv.invoice_date  # Store intermediate finance value.
        statement.aging[_bucket_for((end_date - ref_date).days)] += due  # Store intermediate finance value.

    return statement  # Return the computed finance response.


# --------------------------------------------------------------------------- #
# Budget vs actual                                                            #
# --------------------------------------------------------------------------- #


@dataclass  # Decorator configures the following callable.
class BudgetVarianceRow:  # Class groups related finance API or service behavior.
    """Budget vs actual for one account (kobo), signed to the account's normal balance.

    ``variance = actual - budget``. Reading it depends on the account: for an expense
    a positive variance is *over* budget (unfavourable); for income it is *over* plan
    (favourable). The report stays neutral and just reports the signed numbers.
    """

    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    budget: int  # Finance processing step.
    actual: int  # Finance processing step.

    @property  # Decorator configures the following callable.
    def variance(self) -> int:  # Function handles this finance operation.
        return self.actual - self.budget  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def variance_pct(self) -> float | None:  # Function handles this finance operation.
        """Variance as a percentage of budget, or ``None`` when nothing was budgeted."""
        if self.budget == 0:  # Branch when this finance condition is true.
            return None  # Return the computed finance response.
        return round(self.variance * 100 / self.budget, 2)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class BudgetVarianceReport:  # Class groups related finance API or service behavior.
    budget_id: int  # Finance processing step.
    fiscal_year_id: int  # Finance processing step.
    period_no: int | None  # Finance processing step.
    rows: list = field(default_factory=list)  # Store intermediate finance value.
    total_budget: int = 0  # Store intermediate finance value.
    total_actual: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def total_variance(self) -> int:  # Function handles this finance operation.
        return self.total_actual - self.total_budget  # Return the computed finance response.


def budget_vs_actual(budget, *, period_no=None) -> BudgetVarianceReport:  # Function handles this finance operation.
    """Compare a budget's planned figures to ledger actuals, per account.

    Budgeted amounts come from the (frozen) :class:`~vs_finance.models.BudgetLine`
    cells; actuals come from the denormalised :class:`AccountBalance` *movement* in
    the matching fiscal periods (period movement only — opening balances are
    excluded), signed to each account's normal balance so an expense budget of
    ``100`` lines up with ``100`` of actual expense. Pass ``period_no`` (1–12) to
    scope both sides to a single period; otherwise the whole fiscal year is summed.
    """
    from .constants import AccountType, NormalBalance  # Import dependency used by this finance module.
    from .models import AccountBalance, BudgetLine  # Import dependency used by this finance module.

    # Budgets are plans of income/expense; the balance-sheet contra side of a posting
    # (cash, AR, payables) is noise in a variance report, so unbudgeted accounts only
    # appear when they are P&L accounts (i.e. genuinely unbudgeted income/spend).
    _PL_TYPES = {AccountType.INCOME, AccountType.EXPENSE}  # Store intermediate finance value.

    fiscal_year = budget.fiscal_year  # Store intermediate finance value.

    # Budgeted amounts per account (summed across cost centres / periods).
    budget_lines = BudgetLine.objects.filter(budget=budget).select_related("account")  # Query finance data from the database.
    if period_no is not None:  # Branch when this finance condition is true.
        budget_lines = budget_lines.filter(period_no=int(period_no))  # Store intermediate finance value.

    slots: dict[int, dict] = {}  # Store intermediate finance value.

    def slot_for(account):  # Function handles this finance operation.
        s = slots.get(account.id)  # Store intermediate finance value.
        if s is None:  # Branch when this finance condition is true.
            s = {  # Store intermediate finance value.
                "code": account.code, "name": account.name,  # Finance processing step.
                "account_type": account.account_type,  # Finance processing step.
                "normal_balance": account.normal_balance,  # Finance processing step.
                "budget": 0, "actual": 0,  # Finance processing step.
            }  # Continue structured finance payload.
            slots[account.id] = s  # Store intermediate finance value.
        return s  # Return the computed finance response.

    for line in budget_lines:  # Iterate through finance records.
        slot_for(line.account)["budget"] += line.amount  # Store intermediate finance value.

    # Actual movement per account from the period balances of this fiscal year.
    balances = (  # Store intermediate finance value.
        AccountBalance.objects  # Query finance data from the database.
        .filter(period__fiscal_year=fiscal_year)  # Store intermediate finance value.
        .select_related("account", "period")  # Finance processing step.
    )  # Continue structured finance payload.
    if period_no is not None:  # Branch when this finance condition is true.
        balances = balances.filter(period__period_no=int(period_no))  # Store intermediate finance value.

    for bal in balances:  # Iterate through finance records.
        acc = bal.account  # Store intermediate finance value.
        # An unbudgeted, non-P&L account (e.g. the cash contra side) is not part of a
        # budget variance — only surface budgeted accounts and unbudgeted P&L activity.
        if acc.id not in slots and acc.account_type not in _PL_TYPES:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        movement = bal.debit_total - bal.credit_total  # Store intermediate finance value.
        if acc.normal_balance != NormalBalance.DEBIT:  # Branch when this finance condition is true.
            movement = -movement  # Store intermediate finance value.
        if movement == 0 and acc.id not in slots:  # Branch when this finance condition is true.
            continue  # untouched, unbudgeted account — skip the noise
        slot_for(acc)["actual"] += movement  # Store intermediate finance value.

    rows: list[BudgetVarianceRow] = []  # Store intermediate finance value.
    total_budget = 0  # Store intermediate finance value.
    total_actual = 0  # Store intermediate finance value.
    for account_id, slot in sorted(slots.items(), key=lambda kv: kv[1]["code"]):  # Iterate through finance records.
        if slot["budget"] == 0 and slot["actual"] == 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        total_budget += slot["budget"]  # Store intermediate finance value.
        total_actual += slot["actual"]  # Store intermediate finance value.
        rows.append(  # Finance processing step.
            BudgetVarianceRow(  # Finance processing step.
                account_id=account_id, code=slot["code"], name=slot["name"],  # Store intermediate finance value.
                account_type=slot["account_type"],  # Store intermediate finance value.
                budget=slot["budget"], actual=slot["actual"],  # Store intermediate finance value.
            )  # Continue structured finance payload.
        )  # Continue structured finance payload.

    return BudgetVarianceReport(  # Return the computed finance response.
        budget_id=budget.id,  # Store intermediate finance value.
        fiscal_year_id=fiscal_year.id,  # Store intermediate finance value.
        period_no=int(period_no) if period_no is not None else None,  # Store intermediate finance value.
        rows=rows,  # Store intermediate finance value.
        total_budget=total_budget,  # Store intermediate finance value.
        total_actual=total_actual,  # Store intermediate finance value.
    )  # Continue structured finance payload.


@dataclass  # Decorator configures the following callable.
class BudgetMatrixRow:  # Class groups related finance API or service behavior.
    """One account's budget-vs-actual across the 12 periods (the heatmap row).

    ``cells`` is always 12 entries keyed ``budget_<n>`` / ``actual_<n>`` for period
    ``n`` (1–12), signed to the account's normal balance like :func:`budget_vs_actual`.
    """

    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    cells: list  # [{period_no, budget, actual}] × 12
    budget_total: int = 0  # Store intermediate finance value.
    actual_total: int = 0  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class BudgetMatrix:  # Class groups related finance API or service behavior.
    budget_id: int  # Finance processing step.
    fiscal_year_id: int  # Finance processing step.
    periods: list = field(default_factory=list)  # [{period_no, label}]
    rows: list = field(default_factory=list)  # Store intermediate finance value.
    total_budget: int = 0  # Store intermediate finance value.
    total_actual: int = 0  # Store intermediate finance value.


def budget_monthly_matrix(budget) -> BudgetMatrix:  # Function handles this finance operation.
    """Per-account, per-period budget vs actual for a budget — the variance heatmap.

    One row per account (budgeted accounts + unbudgeted P&L activity), each with 12
    period cells. Built in two passes (budget lines, then period balances) — no
    per-cell query — so the whole grid is one cheap read.
    """
    from .constants import AccountType, NormalBalance  # Import dependency used by this finance module.
    from .models import AccountBalance, BudgetLine, FiscalPeriod  # Import dependency used by this finance module.

    _PL_TYPES = {AccountType.INCOME, AccountType.EXPENSE}  # Store intermediate finance value.
    fiscal_year = budget.fiscal_year  # Store intermediate finance value.

    periods = list(  # Store intermediate finance value.
        FiscalPeriod.objects.filter(fiscal_year=fiscal_year, period_no__lte=12)  # Query finance data from the database.
        .order_by("period_no")  # Finance processing step.
    )  # Continue structured finance payload.
    period_nos = [p.period_no for p in periods]  # Store intermediate finance value.

    slots: dict[int, dict] = {}  # Store intermediate finance value.

    def slot_for(account):  # Function handles this finance operation.
        s = slots.get(account.id)  # Store intermediate finance value.
        if s is None:  # Branch when this finance condition is true.
            s = {  # Store intermediate finance value.
                "code": account.code, "name": account.name,  # Finance processing step.
                "account_type": account.account_type,  # Finance processing step.
                "normal_balance": account.normal_balance,  # Finance processing step.
                "budget": {n: 0 for n in period_nos},  # Finance processing step.
                "actual": {n: 0 for n in period_nos},  # Finance processing step.
            }  # Continue structured finance payload.
            slots[account.id] = s  # Store intermediate finance value.
        return s  # Return the computed finance response.

    for line in BudgetLine.objects.filter(budget=budget).select_related("account"):  # Iterate through finance records.
        if line.period_no in period_nos:  # Branch when this finance condition is true.
            slot_for(line.account)["budget"][line.period_no] += line.amount  # Store intermediate finance value.

    balances = (  # Store intermediate finance value.
        AccountBalance.objects  # Query finance data from the database.
        .filter(period__fiscal_year=fiscal_year, period__period_no__lte=12)  # Store intermediate finance value.
        .select_related("account", "period")  # Finance processing step.
    )  # Continue structured finance payload.
    for bal in balances:  # Iterate through finance records.
        acc = bal.account  # Store intermediate finance value.
        if acc.id not in slots and acc.account_type not in _PL_TYPES:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        movement = bal.debit_total - bal.credit_total  # Store intermediate finance value.
        if acc.normal_balance != NormalBalance.DEBIT:  # Branch when this finance condition is true.
            movement = -movement  # Store intermediate finance value.
        if movement == 0 and acc.id not in slots:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        slot_for(acc)["actual"][bal.period.period_no] += movement  # Store intermediate finance value.

    rows: list[BudgetMatrixRow] = []  # Store intermediate finance value.
    grand_budget = grand_actual = 0  # Store intermediate finance value.
    for account_id, slot in sorted(slots.items(), key=lambda kv: kv[1]["code"]):  # Iterate through finance records.
        cells = [  # Store intermediate finance value.
            {"period_no": n, "budget": slot["budget"][n], "actual": slot["actual"][n]}  # Continue structured finance payload.
            for n in period_nos  # Iterate through finance records.
        ]  # Continue structured finance payload.
        b_total = sum(slot["budget"].values())  # Store intermediate finance value.
        a_total = sum(slot["actual"].values())  # Store intermediate finance value.
        if b_total == 0 and a_total == 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        grand_budget += b_total  # Store intermediate finance value.
        grand_actual += a_total  # Store intermediate finance value.
        rows.append(BudgetMatrixRow(  # Finance processing step.
            account_id=account_id, code=slot["code"], name=slot["name"],  # Store intermediate finance value.
            account_type=slot["account_type"], cells=cells,  # Store intermediate finance value.
            budget_total=b_total, actual_total=a_total,  # Store intermediate finance value.
        ))  # Continue structured finance payload.

    return BudgetMatrix(  # Return the computed finance response.
        budget_id=budget.id,  # Store intermediate finance value.
        fiscal_year_id=fiscal_year.id,  # Store intermediate finance value.
        periods=[{"period_no": p.period_no, "label": p.name} for p in periods],  # Store intermediate finance value.
        rows=rows,  # Store intermediate finance value.
        total_budget=grand_budget,  # Store intermediate finance value.
        total_actual=grand_actual,  # Store intermediate finance value.
    )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Financial statements — Income Statement, Balance Sheet, Cash Flow            #
# --------------------------------------------------------------------------- #
#
# The three primary statements, all read from the same denormalised
# ``AccountBalance`` aggregates (the cash-flow statement additionally scans posted
# journal lines to classify cash movements). The cardinal links they demonstrate:
#
#   * Income Statement net income, for the open year, is *unclosed* — it has not yet
#     been journalled into Retained Earnings. The Balance Sheet therefore folds that
#     same net income into equity, which is exactly why ``assets == liabilities +
#     equity`` holds before the year is closed.
#   * The Cash Flow statement reconciles ``opening cash + net change == closing cash``;
#     because every journal balances, the non-cash legs of every cash-touching entry
#     sum to the cash movement, so the classified buckets always foot to net change.


@dataclass  # Decorator configures the following callable.
class StatementLine:  # Class groups related finance API or service behavior.
    """One account's contribution to a statement (kobo), signed to its normal balance."""

    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    amount: int  # Finance processing step.

    @property  # Decorator configures the following callable.
    def amount_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.amount)  # Return the computed finance response.


def _net_by_account(balances, *, account_types=None) -> dict:  # Function handles this finance operation.
    """Aggregate ``AccountBalance`` rows into ``{account_id: (account, net_kobo)}``.

    ``net`` is the closing position (opening + movement) signed to the account **type's**
    natural side (ASSET/EXPENSE → debit-positive; LIABILITY/EQUITY/INCOME →
    credit-positive) — *not* the account's own ``normal_balance``. That distinction
    matters for **contra** accounts: accumulated depreciation (a contra-asset) carries a
    credit balance, so signing it as an asset (dr − cr) makes it *reduce* PP&E on the
    statements, and a contra-income (sales returns) reduces revenue. Signing by the
    account's own (flipped) normal balance would instead *add* these, overstating the
    line and breaking the balance-sheet equation. Pass ``account_types`` (a set of
    :class:`AccountType`) to restrict which accounts count.
    """
    from .constants import AccountType  # Import dependency used by this finance module.

    debit_natural = {AccountType.ASSET, AccountType.EXPENSE}  # Store intermediate finance value.

    out: dict[int, list] = {}  # Store intermediate finance value.
    for bal in balances:  # Iterate through finance records.
        acc = bal.account  # Store intermediate finance value.
        if account_types is not None and acc.account_type not in account_types:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        dr = bal.opening_debit + bal.debit_total  # Store intermediate finance value.
        cr = bal.opening_credit + bal.credit_total  # Store intermediate finance value.
        net = (dr - cr) if acc.account_type in debit_natural else (cr - dr)  # Store intermediate finance value.
        slot = out.get(acc.id)  # Store intermediate finance value.
        if slot is None:  # Branch when this finance condition is true.
            out[acc.id] = [acc, net]  # Store intermediate finance value.
        else:  # Fallback finance branch.
            slot[1] += net  # Store intermediate finance value.
    return out  # Return the computed finance response.


def _statement_rows(net_map) -> tuple[list, int]:  # Function handles this finance operation.
    """Turn a ``{account_id: (account, net)}`` map into sorted rows + their total."""
    rows: list[StatementLine] = []  # Store intermediate finance value.
    total = 0  # Store intermediate finance value.
    for _aid, (acc, net) in sorted(net_map.items(), key=lambda kv: kv[1][0].code):  # Iterate through finance records.
        if net == 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        total += net  # Store intermediate finance value.
        rows.append(StatementLine(  # Finance processing step.
            account_id=acc.id, code=acc.code, name=acc.name,  # Store intermediate finance value.
            account_type=acc.account_type, amount=net,  # Store intermediate finance value.
        ))  # Continue structured finance payload.
    return rows, total  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class IncomeStatement:  # Class groups related finance API or service behavior.
    """Revenue less expenses for a window → net income (kobo).

    ``net_income = total_income - total_expense``. Both totals are signed to their
    accounts' normal balance (income credit-natural, expense debit-natural), so both
    are reported as positive magnitudes and the subtraction reads naturally.
    """

    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    income_rows: list = field(default_factory=list)  # Store intermediate finance value.
    expense_rows: list = field(default_factory=list)  # Store intermediate finance value.
    total_income: int = 0  # Store intermediate finance value.
    total_expense: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def net_income(self) -> int:  # Function handles this finance operation.
        return self.total_income - self.total_expense  # Return the computed finance response.


def income_statement(entity, *, period=None) -> IncomeStatement:  # Function handles this finance operation.
    """Build the income statement (P&L) for ``entity``, optionally one ``period``.

    Sums INCOME and EXPENSE accounts from :class:`AccountBalance`. When ``period`` is
    given only that period's balances count; otherwise every period is aggregated
    (year/life-to-date). The result's ``net_income`` is what the Balance Sheet folds
    into equity until the year is closed to Retained Earnings.
    """
    from .constants import AccountType  # Import dependency used by this finance module.
    from .models import AccountBalance  # Import dependency used by this finance module.

    qs = AccountBalance.objects.filter(account__entity=entity).select_related("account")  # Query finance data from the database.
    if period is not None:  # Branch when this finance condition is true.
        qs = qs.filter(period=period)  # Store intermediate finance value.

    income = _net_by_account(qs, account_types={AccountType.INCOME})  # Store intermediate finance value.
    expense = _net_by_account(qs, account_types={AccountType.EXPENSE})  # Store intermediate finance value.
    income_rows, total_income = _statement_rows(income)  # Store intermediate finance value.
    expense_rows, total_expense = _statement_rows(expense)  # Store intermediate finance value.

    return IncomeStatement(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        income_rows=income_rows,  # Store intermediate finance value.
        expense_rows=expense_rows,  # Store intermediate finance value.
        total_income=total_income,  # Store intermediate finance value.
        total_expense=total_expense,  # Store intermediate finance value.
    )  # Continue structured finance payload.


@dataclass  # Decorator configures the following callable.
class ISCompareLine:  # Class groups related finance API or service behavior.
    """One P&L account row with its comparison figures (all kobo; None = not available)."""
    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    account_type: str  # Finance processing step.
    amount: int = 0  # Store intermediate finance value.
    budget: int | None = None  # Store intermediate finance value.
    variance: int | None = None  # Store intermediate finance value.
    prior_year: int | None = None  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class ISCompareTotals:  # Class groups related finance API or service behavior.
    amount: int = 0  # Store intermediate finance value.
    budget: int | None = None  # Store intermediate finance value.
    variance: int | None = None  # Store intermediate finance value.
    prior_year: int | None = None  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class IncomeStatementCompare:  # Class groups related finance API or service behavior.
    """The income statement with optional Budget and Prior-year columns.

    Unlike :func:`income_statement` (which sums income/expense across *all* periods),
    this is **fiscal-year scoped**: "this period" is the current fiscal year (or one
    period of it), so the Prior-year column — the same scope in the previous fiscal
    year — is a like-for-like comparison. Budget comes from the entity's budget for the
    current fiscal year. Variance is signed *favourable* (revenue: actual − budget;
    expense: budget − actual; net income: actual − budget).
    """
    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    period_name: str | None  # Finance processing step.
    fiscal_year: int | None  # Finance processing step.
    prior_fiscal_year: int | None  # Finance processing step.
    has_budget: bool  # Finance processing step.
    has_prior_year: bool  # Finance processing step.
    income_rows: list = field(default_factory=list)  # Store intermediate finance value.
    expense_rows: list = field(default_factory=list)  # Store intermediate finance value.
    income_totals: ISCompareTotals = field(default_factory=ISCompareTotals)  # Store intermediate finance value.
    expense_totals: ISCompareTotals = field(default_factory=ISCompareTotals)  # Store intermediate finance value.
    net_totals: ISCompareTotals = field(default_factory=ISCompareTotals)  # Store intermediate finance value.


def income_statement_compare(entity, *, period=None) -> IncomeStatementCompare:  # Function handles this finance operation.
    """Build the income statement with Budget + Prior-year comparison columns.

    Scope is a **fiscal year**: ``period`` (a :class:`FiscalPeriod`) narrows both this
    year and the prior year to that single period number; otherwise the whole current
    fiscal year (the latest) is used. See :class:`IncomeStatementCompare`.
    """
    from .constants import AccountType, BudgetStatus  # Import dependency used by this finance module.
    from .models import AccountBalance, Budget, BudgetLine, FiscalYear  # Import dependency used by this finance module.

    fy = period.fiscal_year if period is not None else (  # Store intermediate finance value.
        FiscalYear.objects.filter(entity=entity).order_by("-year").first())  # Query finance data from the database.
    if fy is None:  # Branch when this finance condition is true.
        return IncomeStatementCompare(  # Return the computed finance response.
            entity_id=entity.id, period_id=None, period_name=None,  # Store intermediate finance value.
            fiscal_year=None, prior_fiscal_year=None,  # Store intermediate finance value.
            has_budget=False, has_prior_year=False)  # Store intermediate finance value.

    period_no = period.period_no if period is not None else None  # Store intermediate finance value.

    def _actuals(fiscal_year):  # Function handles this finance operation.
        qs = AccountBalance.objects.filter(  # Query finance data from the database.
            account__entity=entity, period__fiscal_year=fiscal_year,  # Store intermediate finance value.
        ).select_related("account")  # Continue structured finance payload.
        if period_no is not None:  # Branch when this finance condition is true.
            qs = qs.filter(period__period_no=period_no)  # Store intermediate finance value.
        return (_net_by_account(qs, account_types={AccountType.INCOME}),  # Return the computed finance response.
                _net_by_account(qs, account_types={AccountType.EXPENSE}))  # Store intermediate finance value.

    cur_inc, cur_exp = _actuals(fy)  # Store intermediate finance value.

    prior_fy = FiscalYear.objects.filter(entity=entity, year=fy.year - 1).first()  # Query finance data from the database.
    has_prior = prior_fy is not None  # Store intermediate finance value.
    pri_inc, pri_exp = _actuals(prior_fy) if has_prior else ({}, {})  # Store intermediate finance value.

    # Budget for the current fiscal year — prefer an approved (locked) plan over a draft.
    budget = (  # Store intermediate finance value.
        Budget.objects.filter(  # Query finance data from the database.
            entity=entity, fiscal_year=fy,  # Store intermediate finance value.
            status=BudgetStatus.APPROVED).order_by("-id").first()  # Store intermediate finance value.
        or Budget.objects.filter(entity=entity, fiscal_year=fy).order_by("-id").first())  # Query finance data from the database.
    has_budget = budget is not None  # Store intermediate finance value.
    budget_by_acc: dict[int, list] = {}  # Store intermediate finance value.
    if has_budget:  # Branch when this finance condition is true.
        blines = BudgetLine.objects.filter(budget=budget).select_related("account")  # Query finance data from the database.
        if period_no is not None:  # Branch when this finance condition is true.
            blines = blines.filter(period_no=period_no)  # Store intermediate finance value.
        for ln in blines:  # Iterate through finance records.
            slot = budget_by_acc.get(ln.account_id)  # Store intermediate finance value.
            if slot is None:  # Branch when this finance condition is true.
                budget_by_acc[ln.account_id] = [ln.account, ln.amount]  # Store intermediate finance value.
            else:  # Fallback finance branch.
                slot[1] += ln.amount  # Store intermediate finance value.

    def _build(cur_map, pri_map, atype, *, revenue):  # Function handles this finance operation.
        accounts: dict[int, object] = {}  # Store intermediate finance value.
        for aid, (acc, _net) in cur_map.items():  # Iterate through finance records.
            accounts[aid] = acc  # Store intermediate finance value.
        for aid, (acc, _net) in pri_map.items():  # Iterate through finance records.
            accounts.setdefault(aid, acc)  # Finance processing step.
        for aid, (acc, _amt) in budget_by_acc.items():  # Iterate through finance records.
            if acc.account_type == atype:  # Branch when this finance condition is true.
                accounts.setdefault(aid, acc)  # Finance processing step.

        rows, tot_amt, tot_bud, tot_pri = [], 0, 0, 0  # Store intermediate finance value.
        for aid, acc in sorted(accounts.items(), key=lambda kv: kv[1].code):  # Iterate through finance records.
            amount = cur_map.get(aid, [None, 0])[1]  # Store intermediate finance value.
            prior = pri_map.get(aid, [None, 0])[1] if has_prior else None  # Store intermediate finance value.
            bud = budget_by_acc.get(aid, [None, 0])[1] if has_budget else None  # Store intermediate finance value.
            if amount == 0 and not bud and not prior:  # Branch when this finance condition is true.
                continue  # Finance processing step.
            var = None  # Store intermediate finance value.
            if has_budget:  # Branch when this finance condition is true.
                var = (amount - bud) if revenue else (bud - amount)  # Store intermediate finance value.
                tot_bud += bud  # Store intermediate finance value.
            if has_prior:  # Branch when this finance condition is true.
                tot_pri += prior or 0  # Store intermediate finance value.
            tot_amt += amount  # Store intermediate finance value.
            rows.append(ISCompareLine(  # Finance processing step.
                account_id=aid, code=acc.code, name=acc.name,  # Store intermediate finance value.
                account_type=acc.account_type, amount=amount,  # Store intermediate finance value.
                budget=bud, variance=var, prior_year=prior))  # Store intermediate finance value.
        totals = ISCompareTotals(  # Store intermediate finance value.
            amount=tot_amt,  # Store intermediate finance value.
            budget=tot_bud if has_budget else None,  # Store intermediate finance value.
            prior_year=tot_pri if has_prior else None,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return rows, totals  # Return the computed finance response.

    income_rows, inc_tot = _build(cur_inc, pri_inc, AccountType.INCOME, revenue=True)  # Store intermediate finance value.
    expense_rows, exp_tot = _build(cur_exp, pri_exp, AccountType.EXPENSE, revenue=False)  # Store intermediate finance value.
    if has_budget:  # Branch when this finance condition is true.
        inc_tot.variance = inc_tot.amount - inc_tot.budget  # Store intermediate finance value.
        exp_tot.variance = exp_tot.budget - exp_tot.amount  # Store intermediate finance value.

    net = ISCompareTotals(  # Store intermediate finance value.
        amount=inc_tot.amount - exp_tot.amount,  # Store intermediate finance value.
        budget=(inc_tot.budget - exp_tot.budget) if has_budget else None,  # Store intermediate finance value.
        prior_year=((inc_tot.prior_year or 0) - (exp_tot.prior_year or 0)) if has_prior else None,  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if has_budget:  # Branch when this finance condition is true.
        net.variance = net.amount - net.budget  # Store intermediate finance value.

    return IncomeStatementCompare(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        period_name=getattr(period, "name", None),  # Store intermediate finance value.
        fiscal_year=fy.year,  # Store intermediate finance value.
        prior_fiscal_year=prior_fy.year if prior_fy else None,  # Store intermediate finance value.
        has_budget=has_budget, has_prior_year=has_prior,  # Store intermediate finance value.
        income_rows=income_rows, expense_rows=expense_rows,  # Store intermediate finance value.
        income_totals=inc_tot, expense_totals=exp_tot, net_totals=net)  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class BalanceSheet:  # Class groups related finance API or service behavior.
    """Assets, liabilities and equity at a point in time (kobo).

    ``retained_earnings`` is the *current* (unclosed) net income folded into equity so
    the accounting equation balances before the year is closed. ``is_balanced`` is the
    headline check: ``total_assets == total_liabilities + total_equity``.
    """

    entity_id: int  # Finance processing step.
    as_of: object  # Finance processing step.
    asset_rows: list = field(default_factory=list)  # Store intermediate finance value.
    liability_rows: list = field(default_factory=list)  # Store intermediate finance value.
    equity_rows: list = field(default_factory=list)  # Store intermediate finance value.
    total_assets: int = 0  # Store intermediate finance value.
    total_liabilities: int = 0  # Store intermediate finance value.
    total_equity_accounts: int = 0  # Store intermediate finance value.
    retained_earnings: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def total_equity(self) -> int:  # Function handles this finance operation.
        """Booked equity accounts plus the unclosed net income for the window."""
        return self.total_equity_accounts + self.retained_earnings  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def is_balanced(self) -> bool:  # Function handles this finance operation.
        return self.total_assets == self.total_liabilities + self.total_equity  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def difference(self) -> int:  # Function handles this finance operation.
        return self.total_assets - (self.total_liabilities + self.total_equity)  # Return the computed finance response.


def balance_sheet(entity, *, as_of=None) -> BalanceSheet:  # Function handles this finance operation.
    """Build the balance sheet for ``entity`` as at ``as_of`` (default: today).

    Aggregates ASSET / LIABILITY / EQUITY balances across every period that has begun
    on or before ``as_of`` (period granularity — partial-period cut-offs are not
    interpolated). The same window's net income (income − expense) is reported as
    ``retained_earnings`` and folded into equity, which is what makes ``assets ==
    liabilities + equity`` hold while the year is still open.
    """
    from .constants import AccountType  # Import dependency used by this finance module.
    from .models import AccountBalance  # Import dependency used by this finance module.

    as_of = as_of or timezone.now().date()  # Store intermediate finance value.

    qs = (  # Store intermediate finance value.
        AccountBalance.objects  # Query finance data from the database.
        .filter(account__entity=entity, period__start_date__lte=as_of)  # Store intermediate finance value.
        .select_related("account")  # Finance processing step.
    )  # Continue structured finance payload.

    assets = _net_by_account(qs, account_types={AccountType.ASSET})  # Store intermediate finance value.
    liabilities = _net_by_account(qs, account_types={AccountType.LIABILITY})  # Store intermediate finance value.
    equity = _net_by_account(qs, account_types={AccountType.EQUITY})  # Store intermediate finance value.

    asset_rows, total_assets = _statement_rows(assets)  # Store intermediate finance value.
    liability_rows, total_liabilities = _statement_rows(liabilities)  # Store intermediate finance value.
    equity_rows, total_equity_accounts = _statement_rows(equity)  # Store intermediate finance value.

    # Unclosed P&L for the same window → folded into equity as retained earnings.
    income = _net_by_account(qs, account_types={AccountType.INCOME})  # Store intermediate finance value.
    expense = _net_by_account(qs, account_types={AccountType.EXPENSE})  # Store intermediate finance value.
    _, total_income = _statement_rows(income)  # Store intermediate finance value.
    _, total_expense = _statement_rows(expense)  # Store intermediate finance value.
    retained = total_income - total_expense  # Store intermediate finance value.

    return BalanceSheet(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        as_of=as_of,  # Store intermediate finance value.
        asset_rows=asset_rows,  # Store intermediate finance value.
        liability_rows=liability_rows,  # Store intermediate finance value.
        equity_rows=equity_rows,  # Store intermediate finance value.
        total_assets=total_assets,  # Store intermediate finance value.
        total_liabilities=total_liabilities,  # Store intermediate finance value.
        total_equity_accounts=total_equity_accounts,  # Store intermediate finance value.
        retained_earnings=retained,  # Store intermediate finance value.
    )  # Continue structured finance payload.


#: Cash-flow activity buckets, in presentation order.
CASH_FLOW_ACTIVITIES = ("operating", "investing", "financing")  # Store intermediate finance value.


def _classify_cash_flow(account) -> str:  # Function handles this finance operation.
    """Bucket a non-cash journal leg into operating / investing / financing.

    A pragmatic, double-entry-safe classification: because every journal balances, the
    non-cash legs of a cash-touching entry always sum to the cash movement, so whatever
    bucket each leg lands in, the three buckets *always* foot to net change in cash.

    * INCOME / EXPENSE and working-capital accounts (current AR / AP) → **operating**
    * non-current ASSET — property, plant & equipment and its accumulated
      depreciation contra → **investing**
    * EQUITY (capital, drawings) → **financing**
    * other LIABILITY (assumed borrowings) → **financing**
    """
    from .constants import (  # Import dependency used by this finance module.
        ACCUM_DEPRECIATION_CODE,  # Finance processing step.
        AccountType,  # Finance processing step.
        PPE_ACCOUNT_CODE,  # Finance processing step.
    )  # Continue structured finance payload.

    atype = account.account_type  # Store intermediate finance value.
    if atype == AccountType.EQUITY:  # Branch when this finance condition is true.
        return "financing"  # Return the computed finance response.
    if atype == AccountType.ASSET:  # Branch when this finance condition is true.
        # Non-current assets (PP&E + accumulated depreciation) are investing flows;
        # everything else (receivables, prepayments) is working-capital → operating.
        if account.code in (PPE_ACCOUNT_CODE, ACCUM_DEPRECIATION_CODE):  # Branch when this finance condition is true.
            return "investing"  # Return the computed finance response.
        return "operating"  # Return the computed finance response.
    if atype == AccountType.LIABILITY:  # Branch when this finance condition is true.
        # Trade payables and accruals are operating working capital; we keep them
        # operating and treat only explicit equity as financing for the default chart.
        return "operating"  # Return the computed finance response.
    # INCOME / EXPENSE
    return "operating"  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class CashFlowLine:  # Class groups related finance API or service behavior.
    """One counter-account's net cash contribution within an activity (kobo).

    ``amount`` is credit − debit on the non-cash leg: positive = cash in, negative =
    cash out (e.g. paying a payable or buying PP&E).
    """

    account_id: int  # Finance processing step.
    code: str  # Finance processing step.
    name: str  # Finance processing step.
    amount: int = 0  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class CashFlowStatement:  # Class groups related finance API or service behavior.
    """Cash movement for a window, classified by activity (kobo).

    ``opening_cash + net_change == closing_cash`` is the reconciliation the statement
    exists to prove. ``by_activity`` holds the operating / investing / financing
    subtotals, which sum to ``net_change``. ``activity_lines`` breaks each activity into
    its counter-account line items (direct method).
    """

    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    opening_cash: int = 0  # Store intermediate finance value.
    closing_cash: int = 0  # Store intermediate finance value.
    by_activity: dict = field(default_factory=lambda: {a: 0 for a in CASH_FLOW_ACTIVITIES})  # Store intermediate finance value.
    activity_lines: dict = field(  # Store intermediate finance value.
        default_factory=lambda: {a: [] for a in CASH_FLOW_ACTIVITIES})  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def net_change(self) -> int:  # Function handles this finance operation.
        return sum(self.by_activity.values())  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def is_reconciled(self) -> bool:  # Function handles this finance operation.
        return self.opening_cash + self.net_change == self.closing_cash  # Return the computed finance response.


def cash_flow_statement(entity, *, period=None) -> CashFlowStatement:  # Function handles this finance operation.
    """Build the cash-flow statement for ``entity``, optionally one ``period``.

    Cash accounts are the entity's ``1100 Cash & Bank`` plus any GL account a
    :class:`~vs_finance.models.BankAccount` maps to. The statement classifies the
    non-cash leg of every POSTED journal that touches cash into operating / investing /
    financing (see :func:`_classify_cash_flow`), and reconciles opening + net change to
    closing cash. Scoped to ``period`` when given, else the whole ledger to date.
    """
    from .constants import CASH_BANK_CODE, DocumentStatus, NormalBalance  # Import dependency used by this finance module.
    from .models import Account, AccountBalance, BankAccount, JournalLine  # Import dependency used by this finance module.

    # 1. Identify the entity's cash accounts (1100 + any mapped bank GL account).
    cash_ids = set(  # Store intermediate finance value.
        Account.objects  # Query finance data from the database.
        .filter(entity=entity, code=CASH_BANK_CODE)  # Store intermediate finance value.
        .values_list("id", flat=True)  # Store intermediate finance value.
    )  # Continue structured finance payload.
    cash_ids |= set(  # Store intermediate finance value.
        BankAccount.objects.filter(entity=entity).values_list("gl_account_id", flat=True)  # Query finance data from the database.
    )  # Continue structured finance payload.

    stmt = CashFlowStatement(entity_id=entity.id, period_id=getattr(period, "id", None))  # Store intermediate finance value.
    if not cash_ids:  # Branch when this finance condition is true.
        return stmt  # Return the computed finance response.

    # 2. Opening / closing cash from the denormalised balances.
    bal_qs = AccountBalance.objects.filter(account_id__in=cash_ids).select_related("account")  # Query finance data from the database.
    if period is not None:  # Branch when this finance condition is true.
        bal_qs = bal_qs.filter(period=period)  # Store intermediate finance value.

    opening = closing = 0  # Store intermediate finance value.
    for bal in bal_qs:  # Iterate through finance records.
        sign = 1 if bal.account.normal_balance == NormalBalance.DEBIT else -1  # Store intermediate finance value.
        open_net = (bal.opening_debit - bal.opening_credit) * sign  # Store intermediate finance value.
        move = (bal.debit_total - bal.credit_total) * sign  # Store intermediate finance value.
        opening += open_net  # Store intermediate finance value.
        closing += open_net + move  # Store intermediate finance value.
    stmt.opening_cash = opening  # Store intermediate finance value.
    stmt.closing_cash = closing  # Store intermediate finance value.

    # 3. Classify the non-cash legs of every posted journal that touches cash.
    cash_entry_ids = set(  # Store intermediate finance value.
        JournalLine.objects  # Query finance data from the database.
        .filter(account_id__in=cash_ids, entry__entity=entity,  # Store intermediate finance value.
                entry__status=DocumentStatus.POSTED)  # Store intermediate finance value.
        .values_list("entry_id", flat=True)  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if period is not None:  # Branch when this finance condition is true.
        cash_entry_ids &= set(  # Store intermediate finance value.
            JournalLine.objects  # Query finance data from the database.
            .filter(entry__period=period, entry_id__in=cash_entry_ids)  # Store intermediate finance value.
            .values_list("entry_id", flat=True)  # Store intermediate finance value.
        )  # Continue structured finance payload.

    legs = (  # Store intermediate finance value.
        JournalLine.objects  # Query finance data from the database.
        .filter(entry_id__in=cash_entry_ids)  # Store intermediate finance value.
        .exclude(account_id__in=cash_ids)  # Store intermediate finance value.
        .select_related("account")  # Finance processing step.
    )  # Continue structured finance payload.
    line_acc: dict[tuple, list] = {}  # Store intermediate finance value.
    for leg in legs:  # Iterate through finance records.
        # A credit to a non-cash account is a source of cash (+), a debit a use (−).
        contribution = leg.credit - leg.debit  # Store intermediate finance value.
        activity = _classify_cash_flow(leg.account)  # Store intermediate finance value.
        stmt.by_activity[activity] += contribution  # Store intermediate finance value.
        slot = line_acc.get((activity, leg.account_id))  # Store intermediate finance value.
        if slot is None:  # Branch when this finance condition is true.
            line_acc[(activity, leg.account_id)] = [leg.account, contribution]  # Store intermediate finance value.
        else:  # Fallback finance branch.
            slot[1] += contribution  # Store intermediate finance value.

    # Break each activity into its counter-account line items (direct method), sorted
    # by account code; net-zero counter-accounts are dropped.
    for (activity, account_id), (acc, amount) in sorted(  # Iterate through finance records.
        line_acc.items(), key=lambda kv: (kv[0][0], kv[1][0].code)  # Store intermediate finance value.
    ):  # Continue structured finance payload.
        if amount == 0:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        stmt.activity_lines[activity].append(CashFlowLine(  # Finance processing step.
            account_id=account_id, code=acc.code, name=acc.name, amount=amount))  # Store intermediate finance value.

    return stmt  # Return the computed finance response.


# --------------------------------------------------------------------------- #
# Statement of Changes in Equity (SOCE)                                        #
# --------------------------------------------------------------------------- #
#
# The fourth primary statement: it reconciles opening to closing equity by
# component (share capital, retained earnings, other reserves), splitting the
# movement into *profit for the period* and *owner contributions / distributions*.
#
# In this ledger the year is never closed into Retained Earnings (P&L sits unclosed
# and the Balance Sheet folds it into equity — see ``balance_sheet``). The SOCE
# mirrors that exactly: each booked EQUITY account becomes a column whose movement in
# the window is a contribution/distribution, and a synthetic *Retained earnings*
# column carries the unclosed net income (opening = cumulative P&L before the window,
# profit = P&L during the window). Closing therefore equals
# ``balance_sheet(as_of=window end).total_equity`` — the invariant ``is_reconciled``
# proves.


#: Key/label for the synthetic retained-earnings (unclosed P&L) column.
RETAINED_EARNINGS_COLUMN = "retained_earnings"  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class EquityMovement:  # Class groups related finance API or service behavior.
    """One equity component's opening → closing walk over a window (kobo).

    ``closing == opening + profit + contributions``. ``profit`` is non-zero only for
    the synthetic retained-earnings column; booked equity accounts move via
    ``contributions`` (share issues +, dividends / drawings −).
    """

    key: str  # Finance processing step.
    label: str  # Finance processing step.
    account_id: int | None = None  # Store intermediate finance value.
    code: str | None = None  # Store intermediate finance value.
    opening: int = 0  # Store intermediate finance value.
    profit: int = 0  # Store intermediate finance value.
    contributions: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def closing(self) -> int:  # Function handles this finance operation.
        return self.opening + self.profit + self.contributions  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def opening_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.opening)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def profit_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.profit)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def contributions_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.contributions)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def closing_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.closing)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class StatementOfChangesInEquity:  # Class groups related finance API or service behavior.
    """Equity reconciliation by component for a window (kobo).

    ``columns`` are the booked equity accounts plus a synthetic retained-earnings
    column. The totals foot the four movement rows; ``is_reconciled`` checks the
    closing total against an independently computed balance-sheet equity at the
    window's end.
    """

    entity_id: int  # Finance processing step.
    period_id: int | None  # Finance processing step.
    as_of: object  # Finance processing step.
    columns: list = field(default_factory=list)  # Store intermediate finance value.
    balance_sheet_equity: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def total_opening(self) -> int:  # Function handles this finance operation.
        return sum(c.opening for c in self.columns)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def total_profit(self) -> int:  # Function handles this finance operation.
        return sum(c.profit for c in self.columns)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def total_contributions(self) -> int:  # Function handles this finance operation.
        return sum(c.contributions for c in self.columns)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def total_closing(self) -> int:  # Function handles this finance operation.
        return sum(c.closing for c in self.columns)  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def is_reconciled(self) -> bool:  # Function handles this finance operation.
        return self.total_closing == self.balance_sheet_equity  # Return the computed finance response.


def _net_income(qs) -> int:  # Function handles this finance operation.
    """Net income (income − expense), signed positive for a profit, over ``qs``."""
    from .constants import AccountType  # Import dependency used by this finance module.

    _, total_income = _statement_rows(_net_by_account(qs, account_types={AccountType.INCOME}))  # Store intermediate finance value.
    _, total_expense = _statement_rows(_net_by_account(qs, account_types={AccountType.EXPENSE}))  # Store intermediate finance value.
    return total_income - total_expense  # Return the computed finance response.


def statement_of_changes_in_equity(entity, *, period=None) -> StatementOfChangesInEquity:  # Function handles this finance operation.
    """Build the statement of changes in equity for ``entity``.

    With ``period`` the window is that single period (opening = every earlier period;
    movement = the period). Without it the window is the whole ledger to date (opening
    zero, everything a movement from inception). Each booked EQUITY account is a
    column; the unclosed P&L is the synthetic retained-earnings column. ``closing``
    reconciles to the balance sheet's equity at the window end.
    """
    from .constants import AccountType  # Import dependency used by this finance module.
    from .models import AccountBalance  # Import dependency used by this finance module.

    base = AccountBalance.objects.filter(account__entity=entity).select_related("account")  # Query finance data from the database.

    if period is not None:  # Branch when this finance condition is true.
        prior_qs = base.filter(period__start_date__lt=period.start_date)  # Store intermediate finance value.
        window_qs = base.filter(period=period)  # Store intermediate finance value.
        as_of = period.end_date  # Store intermediate finance value.
    else:  # Fallback finance branch.
        prior_qs = base.none()  # Store intermediate finance value.
        window_qs = base  # Store intermediate finance value.
        as_of = timezone.now().date()  # Store intermediate finance value.

    opening_map = _net_by_account(prior_qs, account_types={AccountType.EQUITY})  # Store intermediate finance value.
    window_map = _net_by_account(window_qs, account_types={AccountType.EQUITY})  # Store intermediate finance value.

    # One column per booked equity account (union of accounts seen opening or in-window).
    columns: list[EquityMovement] = []  # Store intermediate finance value.
    account_ids = sorted(  # Store intermediate finance value.
        set(opening_map) | set(window_map),  # Finance processing step.
        key=lambda aid: (opening_map.get(aid) or window_map[aid])[0].code,  # Store intermediate finance value.
    )  # Continue structured finance payload.
    for aid in account_ids:  # Iterate through finance records.
        acc = (opening_map.get(aid) or window_map.get(aid))[0]  # Store intermediate finance value.
        columns.append(EquityMovement(  # Finance processing step.
            key=acc.code,  # Store intermediate finance value.
            label=acc.name,  # Store intermediate finance value.
            account_id=acc.id,  # Store intermediate finance value.
            code=acc.code,  # Store intermediate finance value.
            opening=opening_map.get(aid, [None, 0])[1],  # Store intermediate finance value.
            contributions=window_map.get(aid, [None, 0])[1],  # Store intermediate finance value.
        ))  # Continue structured finance payload.

    # Synthetic retained-earnings column: unclosed P&L before vs during the window.
    columns.append(EquityMovement(  # Finance processing step.
        key=RETAINED_EARNINGS_COLUMN,  # Store intermediate finance value.
        label="Retained earnings (unclosed P&L)",  # Store intermediate finance value.
        opening=_net_income(prior_qs),  # Store intermediate finance value.
        profit=_net_income(window_qs),  # Store intermediate finance value.
    ))  # Continue structured finance payload.

    # Independent reconciliation target: balance-sheet equity at the window end.
    bs_equity = balance_sheet(entity, as_of=as_of).total_equity  # Store intermediate finance value.

    return StatementOfChangesInEquity(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        as_of=as_of,  # Store intermediate finance value.
        columns=columns,  # Store intermediate finance value.
        balance_sheet_equity=bs_equity,  # Store intermediate finance value.
    )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Statutory export pack (IFRS-for-SMEs presentation)                          #
# --------------------------------------------------------------------------- #
#
# A single bundle that re-presents the primary statements the way a Nigerian
# statutory filing (FIRS tax computation, CAC annual return) expects: the raw chart
# is regrouped onto IFRS-for-SMEs presentation *lines* (see
# :class:`~vs_finance.constants.IFRSLine`) rather than listed account-by-account.
#
# The pack never re-derives the numbers — it *regroups* the rows the existing
# ``balance_sheet`` and ``income_statement`` already produce, so its section totals
# foot to those statements by construction (``total_assets`` == the balance sheet's
# total assets, etc.) and the accounting equation it asserts is exactly the one the
# balance sheet proves. The cash-flow statement, statement of changes in equity and
# trial balance ride along unchanged as the standard companions / appendix.


#: Statement-of-Financial-Position sections, each an ordered list of its IFRS lines.
#: (key, label, [IFRSLine, …]) — drives both presentation order and section subtotals.
def _ifrs_sofp_sections():  # Function handles this finance operation.
    from .constants import IFRSLine  # Import dependency used by this finance module.
    return [  # Return the computed finance response.
        ("non_current_assets", "Non-current assets",  # Continue structured finance payload.
         [IFRSLine.PPE, IFRSLine.INTANGIBLES, IFRSLine.INVESTMENTS]),  # Continue structured finance payload.
        ("current_assets", "Current assets",  # Continue structured finance payload.
         [IFRSLine.INVENTORIES, IFRSLine.TRADE_RECEIVABLES, IFRSLine.CURRENT_TAX_ASSET,  # Continue structured finance payload.
          IFRSLine.OTHER_CURRENT_ASSETS, IFRSLine.CASH]),  # Finance processing step.
        ("equity", "Equity",  # Continue structured finance payload.
         [IFRSLine.SHARE_CAPITAL, IFRSLine.RETAINED_EARNINGS, IFRSLine.OTHER_RESERVES]),  # Continue structured finance payload.
        ("non_current_liabilities", "Non-current liabilities",  # Continue structured finance payload.
         [IFRSLine.LONG_TERM_BORROWINGS]),  # Continue structured finance payload.
        ("current_liabilities", "Current liabilities",  # Continue structured finance payload.
         [IFRSLine.TRADE_PAYABLES, IFRSLine.CURRENT_TAX_PAYABLE,  # Continue structured finance payload.
          IFRSLine.EMPLOYEE_PAYABLES, IFRSLine.SHORT_TERM_BORROWINGS]),  # Finance processing step.
    ]  # Continue structured finance payload.


#: Income-statement lines in presentation order.
def _ifrs_income_lines():  # Function handles this finance operation.
    from .constants import IFRSLine  # Import dependency used by this finance module.
    return [  # Return the computed finance response.
        IFRSLine.REVENUE, IFRSLine.COST_OF_SALES, IFRSLine.OTHER_INCOME,  # Finance processing step.
        IFRSLine.DISTRIBUTION_COSTS, IFRSLine.ADMIN_EXPENSES, IFRSLine.OTHER_EXPENSES,  # Finance processing step.
        IFRSLine.FINANCE_COSTS, IFRSLine.TAX_EXPENSE,  # Finance processing step.
    ]  # Continue structured finance payload.


def _resolve_ifrs_line(account) -> str:  # Function handles this finance operation.
    """The IFRS-for-SMEs line an account presents on (explicit, else type default)."""
    from .constants import DEFAULT_IFRS_LINE_BY_TYPE  # Import dependency used by this finance module.
    return account.ifrs_line or DEFAULT_IFRS_LINE_BY_TYPE[account.account_type]  # Return the computed finance response.


def _ifrs_line_map(entity) -> dict:  # Function handles this finance operation.
    """``{account_id: resolved_ifrs_line}`` for every account in ``entity``."""
    from .models import Account  # Import dependency used by this finance module.
    return {  # Return the computed finance response.
        a.id: _resolve_ifrs_line(a)  # Finance processing step.
        for a in Account.objects.filter(entity=entity).only(  # Iterate through finance records.
            "id", "ifrs_line", "account_type",  # Finance processing step.
        )  # Continue structured finance payload.
    }  # Continue structured finance payload.


@dataclass  # Decorator configures the following callable.
class IFRSLineGroup:  # Class groups related finance API or service behavior.
    """One IFRS-for-SMEs presentation line: its accounts rolled into a single total."""

    line: str  # Finance processing step.
    label: str  # Finance processing step.
    amount: int = 0  # Store intermediate finance value.
    accounts: list = field(default_factory=list)  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def amount_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.amount)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class IFRSSection:  # Class groups related finance API or service behavior.
    """A statement section (e.g. *Current assets*) and its line subtotals."""

    key: str  # Finance processing step.
    label: str  # Finance processing step.
    groups: list = field(default_factory=list)  # Store intermediate finance value.
    total: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def total_naira(self) -> str:  # Function handles this finance operation.
        return format_naira(self.total)  # Return the computed finance response.


@dataclass  # Decorator configures the following callable.
class StatutoryPack:  # Class groups related finance API or service behavior.
    """An IFRS-for-SMEs statutory pack bundling the primary statements.

    ``sofp_sections`` is the Statement of Financial Position regrouped onto IFRS
    lines; ``income_lines`` the Income Statement likewise. The cash-flow statement,
    statement of changes in equity and trial balance accompany them unchanged.
    Section totals foot to the underlying statements, so ``is_balanced`` mirrors the
    balance sheet's accounting-equation check.
    """

    entity_id: int  # Finance processing step.
    entity_code: str  # Finance processing step.
    as_of: object  # Finance processing step.
    period_id: int | None  # Finance processing step.
    sofp_sections: list = field(default_factory=list)  # Store intermediate finance value.
    income_lines: list = field(default_factory=list)  # Store intermediate finance value.
    total_assets: int = 0  # Store intermediate finance value.
    total_equity: int = 0  # Store intermediate finance value.
    total_liabilities: int = 0  # Store intermediate finance value.
    total_income: int = 0  # Store intermediate finance value.
    total_expense: int = 0  # Store intermediate finance value.
    net_income: int = 0  # Store intermediate finance value.
    cash_flow: object = None  # Store intermediate finance value.
    changes_in_equity: object = None  # Store intermediate finance value.
    trial_balance: object = None  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def is_balanced(self) -> bool:  # Function handles this finance operation.
        return self.total_assets == self.total_equity + self.total_liabilities  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def difference(self) -> int:  # Function handles this finance operation.
        return self.total_assets - (self.total_equity + self.total_liabilities)  # Return the computed finance response.


def _group_rows_by_ifrs_line(rows, line_map, *, ordered_lines, extra=None) -> tuple[list, int]:  # Function handles this finance operation.
    """Roll statement ``rows`` into ordered :class:`IFRSLineGroup` buckets.

    ``rows`` are :class:`StatementLine` objects; ``line_map`` resolves each account to
    its IFRS line. ``extra`` is an optional ``{line: amount}`` of figures with no
    backing account row (e.g. the unclosed retained earnings folded into equity).
    Returns the populated groups (only non-empty lines, in ``ordered_lines`` order)
    and their combined total.
    """
    from .constants import IFRSLine  # Import dependency used by this finance module.

    buckets: dict[str, IFRSLineGroup] = {}  # Store intermediate finance value.
    labels = dict(IFRSLine.choices)  # Store intermediate finance value.

    def bucket(line):  # Function handles this finance operation.
        g = buckets.get(line)  # Store intermediate finance value.
        if g is None:  # Branch when this finance condition is true.
            g = IFRSLineGroup(line=line, label=labels.get(line, line))  # Store intermediate finance value.
            buckets[line] = g  # Store intermediate finance value.
        return g  # Return the computed finance response.

    for row in rows:  # Iterate through finance records.
        line = line_map.get(row.account_id)  # Store intermediate finance value.
        if line is None:  # Branch when this finance condition is true.
            continue  # Finance processing step.
        g = bucket(line)  # Store intermediate finance value.
        g.amount += row.amount  # Store intermediate finance value.
        g.accounts.append({  # Finance processing step.
            "account_id": row.account_id, "code": row.code, "name": row.name,  # Finance processing step.
            "amount": row.amount,  # Finance processing step.
        })  # Continue structured finance payload.

    for line, amount in (extra or {}).items():  # Iterate through finance records.
        if amount:  # Branch when this finance condition is true.
            bucket(line).amount += amount  # Store intermediate finance value.

    groups: list[IFRSLineGroup] = []  # Store intermediate finance value.
    total = 0  # Store intermediate finance value.
    for line in ordered_lines:  # Iterate through finance records.
        g = buckets.get(line)  # Store intermediate finance value.
        if g is None or (g.amount == 0 and not g.accounts):  # Branch when this finance condition is true.
            continue  # Finance processing step.
        total += g.amount  # Store intermediate finance value.
        groups.append(g)  # Finance processing step.
    return groups, total  # Return the computed finance response.


def statutory_pack(entity, *, as_of=None, period=None) -> StatutoryPack:  # Function handles this finance operation.
    """Assemble the IFRS-for-SMEs statutory pack for ``entity``.

    The Statement of Financial Position is taken as at ``as_of`` (default today); the
    Income Statement, cash-flow statement and statement of changes in equity are scoped
    to ``period`` when given (else year/inception-to-date). Every figure is *regrouped*
    from the existing statements, so the pack's totals reconcile to them exactly.
    """
    from .constants import IFRSLine  # Import dependency used by this finance module.

    as_of = as_of or timezone.now().date()  # Store intermediate finance value.
    line_map = _ifrs_line_map(entity)  # Store intermediate finance value.

    # --- Statement of Financial Position (regroup the balance sheet) ---------- #
    bs = balance_sheet(entity, as_of=as_of)  # Store intermediate finance value.
    section_rows = {  # Store intermediate finance value.
        "non_current_assets": bs.asset_rows, "current_assets": bs.asset_rows,  # Finance processing step.
        "equity": bs.equity_rows,  # Finance processing step.
        "non_current_liabilities": bs.liability_rows,  # Finance processing step.
        "current_liabilities": bs.liability_rows,  # Finance processing step.
    }  # Continue structured finance payload.
    sofp_sections: list[IFRSSection] = []  # Store intermediate finance value.
    for key, label, lines in _ifrs_sofp_sections():  # Iterate through finance records.
        # Unclosed P&L is folded into the Retained-earnings equity line, mirroring the
        # balance sheet (which adds it to equity so the equation holds before close).
        extra = {IFRSLine.RETAINED_EARNINGS: bs.retained_earnings} if key == "equity" else None  # Store intermediate finance value.
        groups, total = _group_rows_by_ifrs_line(  # Store intermediate finance value.
            section_rows[key], line_map, ordered_lines=lines, extra=extra,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        sofp_sections.append(IFRSSection(key=key, label=label, groups=groups, total=total))  # Store intermediate finance value.

    section_total = {s.key: s.total for s in sofp_sections}  # Store intermediate finance value.
    total_assets = section_total["non_current_assets"] + section_total["current_assets"]  # Store intermediate finance value.
    total_equity = section_total["equity"]  # Store intermediate finance value.
    total_liabilities = (  # Store intermediate finance value.
        section_total["non_current_liabilities"] + section_total["current_liabilities"]  # Finance processing step.
    )  # Continue structured finance payload.

    # --- Income statement (regroup the P&L) ----------------------------------- #
    pnl = income_statement(entity, period=period)  # Store intermediate finance value.
    income_lines, _ = _group_rows_by_ifrs_line(  # Store intermediate finance value.
        list(pnl.income_rows) + list(pnl.expense_rows), line_map,  # Finance processing step.
        ordered_lines=_ifrs_income_lines(),  # Store intermediate finance value.
    )  # Continue structured finance payload.

    return StatutoryPack(  # Return the computed finance response.
        entity_id=entity.id,  # Store intermediate finance value.
        entity_code=entity.code,  # Store intermediate finance value.
        as_of=as_of,  # Store intermediate finance value.
        period_id=getattr(period, "id", None),  # Store intermediate finance value.
        sofp_sections=sofp_sections,  # Store intermediate finance value.
        income_lines=income_lines,  # Store intermediate finance value.
        total_assets=total_assets,  # Store intermediate finance value.
        total_equity=total_equity,  # Store intermediate finance value.
        total_liabilities=total_liabilities,  # Store intermediate finance value.
        total_income=pnl.total_income,  # Store intermediate finance value.
        total_expense=pnl.total_expense,  # Store intermediate finance value.
        net_income=pnl.net_income,  # Store intermediate finance value.
        cash_flow=cash_flow_statement(entity, period=period),  # Store intermediate finance value.
        changes_in_equity=statement_of_changes_in_equity(entity, period=period),  # Store intermediate finance value.
        trial_balance=trial_balance(entity, period=period),  # Store intermediate finance value.
    )  # Continue structured finance payload.


#: Synthetic equity line for the unclosed net income (no backing GL account).
CURRENT_YEAR_EARNINGS_LINE = "CURRENT_YEAR_EARNINGS"  # Store intermediate finance value.


@dataclass  # Decorator configures the following callable.
class BalanceSheetSections:  # Class groups related finance API or service behavior.
    """The balance sheet grouped into IFRS Statement-of-Financial-Position sections.

    ``sections`` are :class:`IFRSSection` (non-current assets, current assets, equity,
    non-current liabilities, current liabilities). Equity keeps the unclosed net income
    as its own *Current year earnings* line rather than folding it into Retained
    earnings, so it reads like the balance-sheet screen. Totals reconcile to
    :func:`balance_sheet`.
    """

    entity_id: int  # Finance processing step.
    as_of: object  # Finance processing step.
    sections: list = field(default_factory=list)  # Store intermediate finance value.
    total_assets: int = 0  # Store intermediate finance value.
    total_liabilities: int = 0  # Store intermediate finance value.
    total_equity: int = 0  # Store intermediate finance value.
    current_year_earnings: int = 0  # Store intermediate finance value.

    @property  # Decorator configures the following callable.
    def is_balanced(self) -> bool:  # Function handles this finance operation.
        return self.total_assets == self.total_liabilities + self.total_equity  # Return the computed finance response.

    @property  # Decorator configures the following callable.
    def difference(self) -> int:  # Function handles this finance operation.
        return self.total_assets - (self.total_liabilities + self.total_equity)  # Return the computed finance response.


def balance_sheet_sections(entity, *, as_of=None) -> BalanceSheetSections:  # Function handles this finance operation.
    """Regroup the balance sheet onto IFRS SOFP sections for statutory presentation.

    Reuses the same section/line machinery as :func:`statutory_pack`, but surfaces the
    unclosed net income as a distinct *Current year earnings* equity line.
    """
    as_of = as_of or timezone.now().date()  # Store intermediate finance value.
    bs = balance_sheet(entity, as_of=as_of)  # Store intermediate finance value.
    line_map = _ifrs_line_map(entity)  # Store intermediate finance value.

    section_rows = {  # Store intermediate finance value.
        "non_current_assets": bs.asset_rows, "current_assets": bs.asset_rows,  # Finance processing step.
        "equity": bs.equity_rows,  # Finance processing step.
        "non_current_liabilities": bs.liability_rows,  # Finance processing step.
        "current_liabilities": bs.liability_rows,  # Finance processing step.
    }  # Continue structured finance payload.
    sections: list[IFRSSection] = []  # Store intermediate finance value.
    for key, label, lines in _ifrs_sofp_sections():  # Iterate through finance records.
        groups, total = _group_rows_by_ifrs_line(  # Store intermediate finance value.
            section_rows[key], line_map, ordered_lines=lines)  # Store intermediate finance value.
        if key == "equity" and bs.retained_earnings:  # Branch when this finance condition is true.
            groups.append(IFRSLineGroup(  # Finance processing step.
                line=CURRENT_YEAR_EARNINGS_LINE, label="Current year earnings",  # Store intermediate finance value.
                amount=bs.retained_earnings))  # Store intermediate finance value.
            total += bs.retained_earnings  # Store intermediate finance value.
        sections.append(IFRSSection(key=key, label=label, groups=groups, total=total))  # Store intermediate finance value.

    section_total = {s.key: s.total for s in sections}  # Store intermediate finance value.
    return BalanceSheetSections(  # Return the computed finance response.
        entity_id=entity.id, as_of=as_of, sections=sections,  # Store intermediate finance value.
        total_assets=section_total["non_current_assets"] + section_total["current_assets"],  # Store intermediate finance value.
        total_liabilities=(  # Store intermediate finance value.
            section_total["non_current_liabilities"] + section_total["current_liabilities"]),  # Finance processing step.
        total_equity=section_total["equity"],  # Store intermediate finance value.
        current_year_earnings=bs.retained_earnings,  # Store intermediate finance value.
    )  # Continue structured finance payload.
