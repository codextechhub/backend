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


@dataclass
class StatementLine:
    """One account's contribution to a statement (kobo), signed to its normal balance."""

    account_id: int
    code: str
    name: str
    account_type: str
    amount: int

    @property
    def amount_naira(self) -> str:
        return format_naira(self.amount)


def _net_by_account(balances, *, account_types=None) -> dict:
    """Aggregate ``AccountBalance`` rows into ``{account_id: (account, net_kobo)}``.

    ``net`` is the closing position (opening + movement) signed to each account's
    normal balance — positive means the account grew in its natural direction. Pass
    ``account_types`` (a set of :class:`AccountType`) to restrict which accounts count.
    """
    from .constants import NormalBalance

    out: dict[int, list] = {}
    for bal in balances:
        acc = bal.account
        if account_types is not None and acc.account_type not in account_types:
            continue
        dr = bal.opening_debit + bal.debit_total
        cr = bal.opening_credit + bal.credit_total
        net = (dr - cr) if acc.normal_balance == NormalBalance.DEBIT else (cr - dr)
        slot = out.get(acc.id)
        if slot is None:
            out[acc.id] = [acc, net]
        else:
            slot[1] += net
    return out


def _statement_rows(net_map) -> tuple[list, int]:
    """Turn a ``{account_id: (account, net)}`` map into sorted rows + their total."""
    rows: list[StatementLine] = []
    total = 0
    for _aid, (acc, net) in sorted(net_map.items(), key=lambda kv: kv[1][0].code):
        if net == 0:
            continue
        total += net
        rows.append(StatementLine(
            account_id=acc.id, code=acc.code, name=acc.name,
            account_type=acc.account_type, amount=net,
        ))
    return rows, total


@dataclass
class IncomeStatement:
    """Revenue less expenses for a window → net income (kobo).

    ``net_income = total_income - total_expense``. Both totals are signed to their
    accounts' normal balance (income credit-natural, expense debit-natural), so both
    are reported as positive magnitudes and the subtraction reads naturally.
    """

    entity_id: int
    period_id: int | None
    income_rows: list = field(default_factory=list)
    expense_rows: list = field(default_factory=list)
    total_income: int = 0
    total_expense: int = 0

    @property
    def net_income(self) -> int:
        return self.total_income - self.total_expense


def income_statement(entity, *, period=None) -> IncomeStatement:
    """Build the income statement (P&L) for ``entity``, optionally one ``period``.

    Sums INCOME and EXPENSE accounts from :class:`AccountBalance`. When ``period`` is
    given only that period's balances count; otherwise every period is aggregated
    (year/life-to-date). The result's ``net_income`` is what the Balance Sheet folds
    into equity until the year is closed to Retained Earnings.
    """
    from .constants import AccountType
    from .models import AccountBalance

    qs = AccountBalance.objects.filter(account__entity=entity).select_related("account")
    if period is not None:
        qs = qs.filter(period=period)

    income = _net_by_account(qs, account_types={AccountType.INCOME})
    expense = _net_by_account(qs, account_types={AccountType.EXPENSE})
    income_rows, total_income = _statement_rows(income)
    expense_rows, total_expense = _statement_rows(expense)

    return IncomeStatement(
        entity_id=entity.id,
        period_id=getattr(period, "id", None),
        income_rows=income_rows,
        expense_rows=expense_rows,
        total_income=total_income,
        total_expense=total_expense,
    )


@dataclass
class BalanceSheet:
    """Assets, liabilities and equity at a point in time (kobo).

    ``retained_earnings`` is the *current* (unclosed) net income folded into equity so
    the accounting equation balances before the year is closed. ``is_balanced`` is the
    headline check: ``total_assets == total_liabilities + total_equity``.
    """

    entity_id: int
    as_of: object
    asset_rows: list = field(default_factory=list)
    liability_rows: list = field(default_factory=list)
    equity_rows: list = field(default_factory=list)
    total_assets: int = 0
    total_liabilities: int = 0
    total_equity_accounts: int = 0
    retained_earnings: int = 0

    @property
    def total_equity(self) -> int:
        """Booked equity accounts plus the unclosed net income for the window."""
        return self.total_equity_accounts + self.retained_earnings

    @property
    def is_balanced(self) -> bool:
        return self.total_assets == self.total_liabilities + self.total_equity

    @property
    def difference(self) -> int:
        return self.total_assets - (self.total_liabilities + self.total_equity)


def balance_sheet(entity, *, as_of=None) -> BalanceSheet:
    """Build the balance sheet for ``entity`` as at ``as_of`` (default: today).

    Aggregates ASSET / LIABILITY / EQUITY balances across every period that has begun
    on or before ``as_of`` (period granularity — partial-period cut-offs are not
    interpolated). The same window's net income (income − expense) is reported as
    ``retained_earnings`` and folded into equity, which is what makes ``assets ==
    liabilities + equity`` hold while the year is still open.
    """
    from .constants import AccountType
    from .models import AccountBalance

    as_of = as_of or timezone.now().date()

    qs = (
        AccountBalance.objects
        .filter(account__entity=entity, period__start_date__lte=as_of)
        .select_related("account")
    )

    assets = _net_by_account(qs, account_types={AccountType.ASSET})
    liabilities = _net_by_account(qs, account_types={AccountType.LIABILITY})
    equity = _net_by_account(qs, account_types={AccountType.EQUITY})

    asset_rows, total_assets = _statement_rows(assets)
    liability_rows, total_liabilities = _statement_rows(liabilities)
    equity_rows, total_equity_accounts = _statement_rows(equity)

    # Unclosed P&L for the same window → folded into equity as retained earnings.
    income = _net_by_account(qs, account_types={AccountType.INCOME})
    expense = _net_by_account(qs, account_types={AccountType.EXPENSE})
    _, total_income = _statement_rows(income)
    _, total_expense = _statement_rows(expense)
    retained = total_income - total_expense

    return BalanceSheet(
        entity_id=entity.id,
        as_of=as_of,
        asset_rows=asset_rows,
        liability_rows=liability_rows,
        equity_rows=equity_rows,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity_accounts=total_equity_accounts,
        retained_earnings=retained,
    )


#: Cash-flow activity buckets, in presentation order.
CASH_FLOW_ACTIVITIES = ("operating", "investing", "financing")


def _classify_cash_flow(account) -> str:
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
    from .constants import (
        ACCUM_DEPRECIATION_CODE,
        AccountType,
        PPE_ACCOUNT_CODE,
    )

    atype = account.account_type
    if atype == AccountType.EQUITY:
        return "financing"
    if atype == AccountType.ASSET:
        # Non-current assets (PP&E + accumulated depreciation) are investing flows;
        # everything else (receivables, prepayments) is working-capital → operating.
        if account.code in (PPE_ACCOUNT_CODE, ACCUM_DEPRECIATION_CODE):
            return "investing"
        return "operating"
    if atype == AccountType.LIABILITY:
        # Trade payables and accruals are operating working capital; we keep them
        # operating and treat only explicit equity as financing for the default chart.
        return "operating"
    # INCOME / EXPENSE
    return "operating"


@dataclass
class CashFlowStatement:
    """Cash movement for a window, classified by activity (kobo).

    ``opening_cash + net_change == closing_cash`` is the reconciliation the statement
    exists to prove. ``by_activity`` holds the operating / investing / financing
    subtotals, which sum to ``net_change``.
    """

    entity_id: int
    period_id: int | None
    opening_cash: int = 0
    closing_cash: int = 0
    by_activity: dict = field(default_factory=lambda: {a: 0 for a in CASH_FLOW_ACTIVITIES})

    @property
    def net_change(self) -> int:
        return sum(self.by_activity.values())

    @property
    def is_reconciled(self) -> bool:
        return self.opening_cash + self.net_change == self.closing_cash


def cash_flow_statement(entity, *, period=None) -> CashFlowStatement:
    """Build the cash-flow statement for ``entity``, optionally one ``period``.

    Cash accounts are the entity's ``1100 Cash & Bank`` plus any GL account a
    :class:`~vs_finance.models.BankAccount` maps to. The statement classifies the
    non-cash leg of every POSTED journal that touches cash into operating / investing /
    financing (see :func:`_classify_cash_flow`), and reconciles opening + net change to
    closing cash. Scoped to ``period`` when given, else the whole ledger to date.
    """
    from .constants import CASH_BANK_CODE, DocumentStatus, NormalBalance
    from .models import Account, AccountBalance, BankAccount, JournalLine

    # 1. Identify the entity's cash accounts (1100 + any mapped bank GL account).
    cash_ids = set(
        Account.objects
        .filter(entity=entity, code=CASH_BANK_CODE)
        .values_list("id", flat=True)
    )
    cash_ids |= set(
        BankAccount.objects.filter(entity=entity).values_list("gl_account_id", flat=True)
    )

    stmt = CashFlowStatement(entity_id=entity.id, period_id=getattr(period, "id", None))
    if not cash_ids:
        return stmt

    # 2. Opening / closing cash from the denormalised balances.
    bal_qs = AccountBalance.objects.filter(account_id__in=cash_ids).select_related("account")
    if period is not None:
        bal_qs = bal_qs.filter(period=period)

    opening = closing = 0
    for bal in bal_qs:
        sign = 1 if bal.account.normal_balance == NormalBalance.DEBIT else -1
        open_net = (bal.opening_debit - bal.opening_credit) * sign
        move = (bal.debit_total - bal.credit_total) * sign
        opening += open_net
        closing += open_net + move
    stmt.opening_cash = opening
    stmt.closing_cash = closing

    # 3. Classify the non-cash legs of every posted journal that touches cash.
    cash_entry_ids = set(
        JournalLine.objects
        .filter(account_id__in=cash_ids, entry__entity=entity,
                entry__status=DocumentStatus.POSTED)
        .values_list("entry_id", flat=True)
    )
    if period is not None:
        cash_entry_ids &= set(
            JournalLine.objects
            .filter(entry__period=period, entry_id__in=cash_entry_ids)
            .values_list("entry_id", flat=True)
        )

    legs = (
        JournalLine.objects
        .filter(entry_id__in=cash_entry_ids)
        .exclude(account_id__in=cash_ids)
        .select_related("account")
    )
    for leg in legs:
        # A credit to a non-cash account is a source of cash (+), a debit a use (−).
        contribution = leg.credit - leg.debit
        stmt.by_activity[_classify_cash_flow(leg.account)] += contribution

    return stmt
