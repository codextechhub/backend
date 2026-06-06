"""Read-side reporting over the ledger.

These functions only *read* the denormalised :class:`~vs_finance.models.AccountBalance`
aggregates that :mod:`vs_finance.posting` maintains, so they are cheap and never
re-sum the whole journal. The cardinal invariant they exist to demonstrate: a
double-entry ledger's debits and credits are always equal, so a trial balance over a
balanced set of postings **always balances**.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from .money import format_naira


@dataclass
class TrialBalanceRow:
    """One account's debit/credit position on the trial balance (kobo)."""

    account_id: int
    code: str
    name: str
    account_type: str
    debit: int
    credit: int

    @property
    def debit_naira(self) -> str:
        return format_naira(self.debit)

    @property
    def credit_naira(self) -> str:
        return format_naira(self.credit)


@dataclass
class TrialBalance:
    """A trial balance for an entity (optionally a single period).

    ``is_balanced`` is the headline check; in a correct ledger it is always ``True``.
    """

    entity_id: int
    period_id: int | None
    rows: list[TrialBalanceRow] = field(default_factory=list)
    total_debit: int = 0
    total_credit: int = 0

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def difference(self) -> int:
        return self.total_debit - self.total_credit


def trial_balance(entity, *, period=None) -> TrialBalance:
    """Build a trial balance for ``entity``, optionally scoped to one ``period``.

    Each account's net position is reduced to a single side: if accumulated debits
    exceed credits the remainder sits in the debit column, else the credit column —
    the conventional trial-balance presentation. Because every posted journal
    balanced, the column totals are equal.
    """
    from .models import AccountBalance

    qs = AccountBalance.objects.filter(account__entity=entity).select_related("account")
    if period is not None:
        qs = qs.filter(period=period)

    # Aggregate across periods (when not period-scoped) per account.
    by_account: dict[int, dict] = {}
    for bal in qs:
        acc = bal.account
        slot = by_account.setdefault(
            acc.id,
            {
                "code": acc.code, "name": acc.name, "account_type": acc.account_type,
                "debit": 0, "credit": 0,
            },
        )
        slot["debit"] += (bal.opening_debit + bal.debit_total)
        slot["credit"] += (bal.opening_credit + bal.credit_total)

    rows: list[TrialBalanceRow] = []
    total_debit = 0
    total_credit = 0
    for account_id, slot in sorted(by_account.items(), key=lambda kv: kv[1]["code"]):
        net = slot["debit"] - slot["credit"]
        debit = net if net > 0 else 0
        credit = -net if net < 0 else 0
        if debit == 0 and credit == 0:
            continue  # net-zero accounts don't clutter the statement
        total_debit += debit
        total_credit += credit
        rows.append(
            TrialBalanceRow(
                account_id=account_id,
                code=slot["code"], name=slot["name"], account_type=slot["account_type"],
                debit=debit, credit=credit,
            )
        )

    return TrialBalance(
        entity_id=entity.id,
        period_id=getattr(period, "id", None),
        rows=rows,
        total_debit=total_debit,
        total_credit=total_credit,
    )


# --------------------------------------------------------------------------- #
# Accounts-Receivable aging + control reconciliation                          #
# --------------------------------------------------------------------------- #

#: Aging bucket labels, in order. "current" = not yet overdue.
AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def _bucket_for(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


@dataclass
class AgingRow:
    """One customer's outstanding AR, split into aging buckets (kobo)."""

    customer_id: int
    code: str
    name: str
    buckets: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    outstanding: int = 0          # gross of unapplied credit
    unallocated_credit: int = 0   # open payment credit not yet applied
    net: int = 0                  # outstanding - unallocated_credit


@dataclass
class AgingReport:
    entity_id: int
    as_of: object
    rows: list = field(default_factory=list)
    bucket_totals: dict = field(default_factory=lambda: {b: 0 for b in AGING_BUCKETS})
    total_outstanding: int = 0
    total_unallocated_credit: int = 0
    total_net: int = 0


def ar_aging(entity, *, as_of=None) -> AgingReport:
    """Age each customer's open invoices into current/1-30/31-60/61-90/90+ buckets.

    An invoice ages off its ``due_date`` (falling back to ``invoice_date``). Only
    POSTED, not-fully-paid invoices contribute, by their ``balance_due``. Each
    customer's unallocated payment credit is reported and netted, so ``total_net``
    equals the AR control account's GL balance (see :func:`reconcile_ar`).
    """
    from .models import Invoice, Payment

    as_of = as_of or timezone.now().date()
    report = AgingReport(entity_id=entity.id, as_of=as_of)
    rows: dict[int, AgingRow] = {}

    def row_for(customer):
        r = rows.get(customer.id)
        if r is None:
            r = AgingRow(
                customer_id=customer.id, code=customer.code, name=customer.name,
                buckets={b: 0 for b in AGING_BUCKETS},
            )
            rows[customer.id] = r
        return r

    posted_invoices = (
        Invoice.objects
        .filter(entity=entity, status="POSTED")
        .exclude(payment_status="PAID")
        .select_related("customer")
    )
    for inv in posted_invoices:
        due = inv.balance_due
        if due <= 0:
            continue
        ref_date = inv.due_date or inv.invoice_date
        days_overdue = (as_of - ref_date).days
        bucket = _bucket_for(days_overdue)
        r = row_for(inv.customer)
        r.buckets[bucket] += due
        r.outstanding += due

    # Unallocated payment credit reduces a customer's net balance.
    posted_payments = (
        Payment.objects.filter(entity=entity, status="POSTED").select_related("customer")
    )
    for pay in posted_payments:
        credit = pay.unallocated_amount
        if credit <= 0:
            continue
        r = row_for(pay.customer)
        r.unallocated_credit += credit

    for r in rows.values():
        r.net = r.outstanding - r.unallocated_credit
        for b in AGING_BUCKETS:
            report.bucket_totals[b] += r.buckets[b]
        report.total_outstanding += r.outstanding
        report.total_unallocated_credit += r.unallocated_credit
        report.total_net += r.net

    report.rows = sorted(rows.values(), key=lambda x: x.code)
    return report


def _account_gl_net(account) -> int:
    """Net GL movement for an account across all its periods, signed to normal balance."""
    from .constants import NormalBalance

    total = 0
    for bal in account.balances.all():
        dr = bal.opening_debit + bal.debit_total
        cr = bal.opening_credit + bal.credit_total
        total += (dr - cr) if account.normal_balance == NormalBalance.DEBIT else (cr - dr)
    return total


@dataclass
class ARReconciliation:
    entity_id: int
    subledger_total: int     # from the AR aging (customer balances)
    control_total: int       # from the AR control account(s) in the GL
    difference: int

    @property
    def is_reconciled(self) -> bool:
        return self.difference == 0


def reconcile_ar(entity, *, as_of=None) -> ARReconciliation:
    """Assert the AR **sub-ledger** (customer balances) equals the AR **control** GL.

    The cardinal AR control: the sum of what every customer owes must equal the
    balance of the receivable control account(s) in the ledger. Any drift means a
    posting bypassed the sub-ledger (or vice-versa) and must be investigated.
    """
    from .models import Customer

    aging = ar_aging(entity, as_of=as_of)
    subledger_total = aging.total_net

    control_accounts = {
        c.receivable_account
        for c in Customer.objects.filter(entity=entity).select_related("receivable_account")
        if c.receivable_account_id is not None
    }
    control_total = sum(_account_gl_net(acc) for acc in control_accounts)

    return ARReconciliation(
        entity_id=entity.id,
        subledger_total=subledger_total,
        control_total=control_total,
        difference=subledger_total - control_total,
    )


# --------------------------------------------------------------------------- #
# Budget vs actual                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class BudgetVarianceRow:
    """Budget vs actual for one account (kobo), signed to the account's normal balance.

    ``variance = actual - budget``. Reading it depends on the account: for an expense
    a positive variance is *over* budget (unfavourable); for income it is *over* plan
    (favourable). The report stays neutral and just reports the signed numbers.
    """

    account_id: int
    code: str
    name: str
    account_type: str
    budget: int
    actual: int

    @property
    def variance(self) -> int:
        return self.actual - self.budget

    @property
    def variance_pct(self) -> float | None:
        """Variance as a percentage of budget, or ``None`` when nothing was budgeted."""
        if self.budget == 0:
            return None
        return round(self.variance * 100 / self.budget, 2)


@dataclass
class BudgetVarianceReport:
    budget_id: int
    fiscal_year_id: int
    period_no: int | None
    rows: list = field(default_factory=list)
    total_budget: int = 0
    total_actual: int = 0

    @property
    def total_variance(self) -> int:
        return self.total_actual - self.total_budget


def budget_vs_actual(budget, *, period_no=None) -> BudgetVarianceReport:
    """Compare a budget's planned figures to ledger actuals, per account.

    Budgeted amounts come from the (frozen) :class:`~vs_finance.models.BudgetLine`
    cells; actuals come from the denormalised :class:`AccountBalance` *movement* in
    the matching fiscal periods (period movement only — opening balances are
    excluded), signed to each account's normal balance so an expense budget of
    ``100`` lines up with ``100`` of actual expense. Pass ``period_no`` (1–12) to
    scope both sides to a single period; otherwise the whole fiscal year is summed.
    """
    from .constants import AccountType, NormalBalance
    from .models import AccountBalance, BudgetLine

    # Budgets are plans of income/expense; the balance-sheet contra side of a posting
    # (cash, AR, payables) is noise in a variance report, so unbudgeted accounts only
    # appear when they are P&L accounts (i.e. genuinely unbudgeted income/spend).
    _PL_TYPES = {AccountType.INCOME, AccountType.EXPENSE}

    fiscal_year = budget.fiscal_year

    # Budgeted amounts per account (summed across cost centres / periods).
    budget_lines = BudgetLine.objects.filter(budget=budget).select_related("account")
    if period_no is not None:
        budget_lines = budget_lines.filter(period_no=int(period_no))

    slots: dict[int, dict] = {}

    def slot_for(account):
        s = slots.get(account.id)
        if s is None:
            s = {
                "code": account.code, "name": account.name,
                "account_type": account.account_type,
                "normal_balance": account.normal_balance,
                "budget": 0, "actual": 0,
            }
            slots[account.id] = s
        return s

    for line in budget_lines:
        slot_for(line.account)["budget"] += line.amount

    # Actual movement per account from the period balances of this fiscal year.
    balances = (
        AccountBalance.objects
        .filter(period__fiscal_year=fiscal_year)
        .select_related("account", "period")
    )
    if period_no is not None:
        balances = balances.filter(period__period_no=int(period_no))

    for bal in balances:
        acc = bal.account
        # An unbudgeted, non-P&L account (e.g. the cash contra side) is not part of a
        # budget variance — only surface budgeted accounts and unbudgeted P&L activity.
        if acc.id not in slots and acc.account_type not in _PL_TYPES:
            continue
        movement = bal.debit_total - bal.credit_total
        if acc.normal_balance != NormalBalance.DEBIT:
            movement = -movement
        if movement == 0 and acc.id not in slots:
            continue  # untouched, unbudgeted account — skip the noise
        slot_for(acc)["actual"] += movement

    rows: list[BudgetVarianceRow] = []
    total_budget = 0
    total_actual = 0
    for account_id, slot in sorted(slots.items(), key=lambda kv: kv[1]["code"]):
        if slot["budget"] == 0 and slot["actual"] == 0:
            continue
        total_budget += slot["budget"]
        total_actual += slot["actual"]
        rows.append(
            BudgetVarianceRow(
                account_id=account_id, code=slot["code"], name=slot["name"],
                account_type=slot["account_type"],
                budget=slot["budget"], actual=slot["actual"],
            )
        )

    return BudgetVarianceReport(
        budget_id=budget.id,
        fiscal_year_id=fiscal_year.id,
        period_no=int(period_no) if period_no is not None else None,
        rows=rows,
        total_budget=total_budget,
        total_actual=total_actual,
    )
