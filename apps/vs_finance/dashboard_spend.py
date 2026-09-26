"""The Cash, spend & compliance view of the finance dashboard.

A third tab beside the overview and receivables, for whoever minds the money
going out and what is owed to the government. It reads the same windows as the
other tabs (see :mod:`vs_finance.dashboard_blocks`), always by date: a term
window here means the days of the term, because cash and spending have no
"billed for".

Cash movement
    The window's cash story from the ledger: the balance of every cash and bank
    account on the window's first day, each kind of money in and out, and the
    balance today. A journal is sorted by what sits on the other side of its cash
    lines (a receivable is a receipt, net wages payable is payroll, a tax
    liability is a remittance, and so on), so the steps need no tagging by the
    person who posted. A journal that only moves money between the school's own
    accounts nets to nothing and is left out.

    Money brought in against equity (capital put in, balances brought forward
    from before the books were kept here) is its own step, so a school that
    starts mid-year does not read its bank balance as a month's income.

Runway
    Cash today against the average monthly cash outflow of the last three
    months: how many months the school could keep paying at that rate if nothing
    more came in. Books with less history than that are averaged over the days
    they have, and books with under two weeks of history get no runway at all.

Spending
    Posted expense in the window, grouped by cost centre when the books tag
    spending with them (untagged spending shows as its own row so the total
    still adds up), and by expense account when they do not.

Access follows the other tabs. The cash, bank, payroll and tax blocks are the
school's money as a whole, so only readers who see the whole school get them.
Spending, budgets, expense claims, petty cash and fixed assets answer under the
reader's branches. A school-wide budget's figures are withheld from a
branch-bound reader, as on the budgets screen: their branches' spending against
the whole plan would read as a shortfall that is only the other branches' share.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from django.db.models import Count, F, Q, Sum

from vs_rbac.scoping import UNNARROWED

from .constants import AccountType, DocumentStatus, InvoicePaymentStatus
from .dashboard_blocks import Window, _m, _ratio, bank_accounts, books_kind, resolve_window

RUNWAY_DAYS = 90
RUNWAY_MIN_DAYS = 14
SPENDING_ROWS = 6
BUDGET_ROWS = 6
OLDEST_CLAIMS = 3
TAX_LOOKBACK_DAYS = 30
TAX_LOOKAHEAD_DAYS = 45

#: Order of the cash movement steps: money in, then money out.
FLOW_ORDER = (
    "receipts", "other_income", "equity", "other_in",
    "payroll", "vendors", "tax", "claims", "petty_cash", "refunds", "spending", "other_out",
)


# --------------------------------------------------------------------------- #
# Windows                                                                     #
# --------------------------------------------------------------------------- #

def _dates(window: Window, as_of: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The days a window covers up to today."""
    return window.start, min(window.end, as_of)


def _same_point_before(entity, window: Window, as_of) -> tuple[datetime.date, datetime.date] | None:
    """The previous window cut to as many days as this one has run, for a fair comparison."""
    from .dashboard_receivables import previous_window

    before = previous_window(entity, window)
    if before is None:
        return None
    start, end = _dates(window, as_of)
    return before.start, min(before.end, before.start + (end - start))


# --------------------------------------------------------------------------- #
# Cash                                                                        #
# --------------------------------------------------------------------------- #

def _account_sets(entity) -> dict[str, set]:
    """The accounts that tell one kind of cash movement from another."""
    from .account_mappings import resolve_mapped_account
    from .constants import (
        ACCRUED_REIMBURSEMENT_CODE,
        NET_WAGES_PAYABLE_CODE,
        AccountMappingKey,
    )
    from .dashboard import _cash_account_ids, _payable_account_ids
    from .models import Account, Customer, ExpenseClaim, PayrollRun, PettyCashFund, TaxObligation

    def mapped(key):
        try:
            return {resolve_mapped_account(entity, key).id}
        except Exception:
            return set()

    codes = dict(Account.objects.filter(entity=entity).values_list("code", "id"))
    types = defaultdict(set)
    for pk, kind in Account.objects.filter(entity=entity).values_list("id", "account_type"):
        types[kind].add(pk)
    return {
        "cash": _cash_account_ids(entity),
        "receivable": set(
            Customer.objects.filter(entity=entity).exclude(receivable_account=None)
            .values_list("receivable_account_id", flat=True)
        ) | mapped(AccountMappingKey.ACCOUNTS_RECEIVABLE) | mapped(AccountMappingKey.CUSTOMER_CREDIT),
        "payroll": ({codes[NET_WAGES_PAYABLE_CODE]} if NET_WAGES_PAYABLE_CODE in codes else set()) | set(
            PayrollRun.objects.filter(entity=entity).exclude(net_payable_account=None)
            .values_list("net_payable_account_id", flat=True)
        ),
        "vendors": _payable_account_ids(entity) | mapped(AccountMappingKey.ACCOUNTS_PAYABLE)
        | mapped(AccountMappingKey.VENDOR_ADVANCE),
        "tax": set(
            TaxObligation.objects.filter(entity=entity).values_list("liability_account_id", flat=True)
        ) - {None},
        "claims": ({codes[ACCRUED_REIMBURSEMENT_CODE]} if ACCRUED_REIMBURSEMENT_CODE in codes else set()) | set(
            ExpenseClaim.objects.filter(entity=entity).exclude(reimbursement_account=None)
            .values_list("reimbursement_account_id", flat=True)
        ),
        "petty_cash": set(PettyCashFund.objects.filter(entity=entity).values_list("gl_account_id", flat=True)),
        "income": types[AccountType.INCOME],
        "equity": types[AccountType.EQUITY],
        "expense": types[AccountType.EXPENSE],
    }


def _classify(net: int, counter: set, sets: dict) -> str:
    """What kind of cash movement a journal is, from the accounts on its other side."""
    if net > 0:
        if counter & sets["receivable"]:
            return "receipts"
        if counter & sets["income"]:
            return "other_income"
        if counter & sets["equity"]:
            return "equity"
        return "other_in"
    for key in ("payroll", "vendors", "tax", "claims", "petty_cash"):
        if counter & sets[key]:
            return key
    if counter & sets["receivable"]:
        return "refunds"
    if counter & sets["expense"]:
        return "spending"
    return "other_out"


def _cash_flows(entity, start, end, sets) -> dict[str, int]:
    """Net cash moved in ``[start, end]``, by kind of movement."""
    from .branch_ledger import LEDGER_STATUSES
    from .models import JournalLine

    touching = JournalLine.objects.filter(
        entry__entity=entity, entry__status__in=LEDGER_STATUSES,
        entry__date__gte=start, entry__date__lte=end, account_id__in=sets["cash"],
    ).values("entry_id")
    net, counter = defaultdict(int), defaultdict(set)
    for entry_id, account_id, debit, credit in JournalLine.objects.filter(
        entry_id__in=touching,
    ).values_list("entry_id", "account_id", "debit", "credit"):
        if account_id in sets["cash"]:
            net[entry_id] += debit - credit
        else:
            counter[entry_id].add(account_id)
    flows = defaultdict(int)
    for entry_id, amount in net.items():
        if amount:
            flows[_classify(amount, counter[entry_id], sets)] += amount
    return flows


def _cash_on(entity, day, cash_ids) -> int:
    """The balance of the cash accounts at the start of ``day``."""
    from .branch_ledger import LEDGER_STATUSES
    from .models import JournalLine

    agg = JournalLine.objects.filter(
        entry__entity=entity, entry__status__in=LEDGER_STATUSES,
        entry__date__lt=day, account_id__in=cash_ids,
    ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
    return int(agg["dr"] or 0) - int(agg["cr"] or 0)


def cash_movement(entity, window: Window, as_of, sets) -> dict:
    """Opening cash, each kind of money in and out over the window, and cash today."""
    start, end = _dates(window, as_of)
    opening = _cash_on(entity, start, sets["cash"])
    flows = _cash_flows(entity, start, end, sets)
    steps = [{"key": k, "amount": _m(flows[k])} for k in FLOW_ORDER if flows.get(k)]
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "opening": _m(opening),
        "closing": _m(opening + sum(flows.values())),
        "steps": steps,
    }


def runway(entity, as_of, sets, cash_now: int) -> dict:
    """Months of cash at the recent average monthly outflow."""
    from .branch_ledger import LEDGER_STATUSES
    from .models import JournalLine

    first = (
        JournalLine.objects.filter(
            entry__entity=entity, entry__status__in=LEDGER_STATUSES, account_id__in=sets["cash"],
        ).order_by("entry__date").values_list("entry__date", flat=True).first()
    )
    start = max(as_of - datetime.timedelta(days=RUNWAY_DAYS - 1), first or as_of)
    days = (as_of - start).days + 1
    if days < RUNWAY_MIN_DAYS:
        return {"months": None, "monthly_outflow": None, "cash": _m(cash_now), "based_on_days": days}
    flows = _cash_flows(entity, start, as_of, sets)
    monthly = -sum(v for v in flows.values() if v < 0) * 30 // days
    return {
        "months": round(cash_now / monthly, 1) if monthly > 0 and cash_now > 0 else None,
        "monthly_outflow": _m(monthly),
        "cash": _m(cash_now),
        "based_on_days": days,
    }


def reconciliation(banks: list[dict]) -> list[dict]:
    """Each bank account's statement lines: how many are matched, and when it last reconciled."""
    from .models import BankStatementLine

    counts = {
        r["bank_account_id"]: r
        for r in BankStatementLine.objects.filter(bank_account_id__in=[b["id"] for b in banks])
        .values("bank_account_id")
        .annotate(total=Count("id"), open=Count("id", filter=Q(status="UNMATCHED")))
    }
    out = []
    for b in banks:
        c = counts.get(b["id"], {"total": 0, "open": 0})
        out.append({
            "id": b["id"], "name": b["name"], "bank_name": b["bank_name"],
            "lines": c["total"], "matched": c["total"] - c["open"],
            "unmatched": b["unmatched_lines"], "unmatched_amount": b["unmatched_amount"],
            "last_reconciled": b["last_reconciled"],
        })
    return out


# --------------------------------------------------------------------------- #
# Spending                                                                    #
# --------------------------------------------------------------------------- #

def _expense_lines(entity, start, end, scope, sets):
    from .branch_ledger import LEDGER_STATUSES
    from .models import JournalLine

    return scope.filter(
        JournalLine.objects.filter(
            entry__entity=entity, entry__status__in=LEDGER_STATUSES,
            entry__date__gte=start, entry__date__lte=end, account_id__in=sets["expense"],
        ),
        "entry__",
    )


def _spent(lines) -> int:
    agg = lines.aggregate(dr=Sum("debit"), cr=Sum("credit"))
    return int(agg["dr"] or 0) - int(agg["cr"] or 0)


def operating_spend(entity, window, as_of, sets, scope=UNNARROWED) -> dict:
    """Posted expense in the window, against the same point of the window before."""
    from .constants import JournalSource

    start, end = _dates(window, as_of)
    lines = _expense_lines(entity, start, end, scope, sets)
    amount = _spent(lines)
    before = _same_point_before(entity, window, as_of)
    previous = _spent(_expense_lines(entity, *before, scope, sets)) if before else None
    payroll = _spent(lines.filter(entry__source=JournalSource.PAYROLL))
    return {
        "amount": _m(amount),
        "previous": _m(previous) if previous is not None else None,
        "delta_pct": round((amount - previous) * 100 / previous, 1) if previous else None,
        "payroll": _m(payroll),
        "payroll_share_pct": _ratio(payroll, amount),
    }


def spending(entity, window, as_of, sets, scope=UNNARROWED) -> dict | None:
    """The window's spending by cost centre, or by expense account when nothing is tagged."""
    start, end = _dates(window, as_of)
    lines = _expense_lines(entity, start, end, scope, sets)
    by_centre = lines.filter(cost_center__isnull=False).exists()
    key, name = ("cost_center_id", "cost_center__name") if by_centre else ("account_id", "account__name")
    rows = [
        (r[name], int(r["dr"] or 0) - int(r["cr"] or 0))
        for r in lines.values(key, name).annotate(dr=Sum("debit"), cr=Sum("credit"))
    ]
    rows = sorted((r for r in rows if r[1] > 0), key=lambda r: -r[1])
    if not rows:
        return None
    total = sum(r[1] for r in rows)
    shown, rest = rows[:SPENDING_ROWS], rows[SPENDING_ROWS:]
    items = [{"name": n or "Not tagged", "amount": _m(a), "share_pct": _ratio(a, total)} for n, a in shown]
    if rest:
        other = sum(a for _, a in rest)
        items.append({"name": f"{len(rest)} more", "amount": _m(other), "share_pct": _ratio(other, total)})
    return {"basis": "cost_centre" if by_centre else "account", "total": _m(total), "items": items}


def budgets(entity, fiscal_year, as_of, scope=UNNARROWED) -> dict | None:
    """This year's plans in the reader's reach, and how much of each is spent."""
    from .models import Budget
    from .reports import budget_vs_actual

    if fiscal_year is None:
        return None
    plans = list(
        scope.filter(Budget.objects.filter(entity=entity, fiscal_year=fiscal_year))
        .select_related("branch").order_by(F("branch__name").asc(nulls_first=True), "name")[:BUDGET_ROWS]
    )
    items = []
    for plan in plans:
        withheld = plan.branch_id is None and scope.is_narrowed
        planned = used = 0
        if not withheld:
            report = budget_vs_actual(plan)
            for row in report.rows:
                if row.account_type == AccountType.EXPENSE:
                    planned += row.budget
                    used += row.actual
        items.append({
            "id": plan.id, "name": plan.name,
            "branch": plan.branch.name if plan.branch_id else None,
            "approved": plan.status == "APPROVED",
            "plan": None if withheld else _m(planned),
            "used": None if withheld else _m(used),
            "pct": None if withheld else _ratio(used, planned),
        })
    span = (fiscal_year.end_date - fiscal_year.start_date).days or 1
    gone = min(max((as_of - fiscal_year.start_date).days, 0), span)
    return {"year_elapsed_pct": round(gone * 100 / span), "items": items}


# --------------------------------------------------------------------------- #
# People and petty cash                                                       #
# --------------------------------------------------------------------------- #

def payroll(entity, as_of) -> dict | None:
    """The latest payroll run due by the end of next month, and what it is made of."""
    from .models import PayrollRun

    run = (
        PayrollRun.objects.filter(entity=entity, pay_date__lte=as_of + datetime.timedelta(days=31))
        .exclude(run_status="CANCELLED")
        .annotate(heads=Count("lines")).order_by("-pay_date", "-id").first()
    )
    if run is None:
        return None
    other = max(run.gross_total - run.paye_total - run.pension_total - run.net_total, 0)
    return {
        "label": run.period_label or run.pay_date.strftime("%B %Y"),
        "pay_date": run.pay_date.isoformat(),
        "status": run.run_status,
        "heads": run.heads,
        "gross": _m(run.gross_total),
        "net": _m(run.net_total),
        "paye": _m(run.paye_total),
        "pension": _m(run.pension_total),
        "other": _m(other),
    }


def expense_claims(entity, window, as_of, sets, scope=UNNARROWED) -> dict:
    """Claims waiting for approval, approved and waiting to be paid, and paid in the window."""
    from .branch_ledger import LEDGER_STATUSES
    from .dashboard import _user_label
    from .models import ExpenseClaim, JournalLine

    claims = scope.filter(ExpenseClaim.objects.filter(entity=entity))
    waiting = claims.filter(status=DocumentStatus.PENDING_APPROVAL)
    approved = claims.filter(status=DocumentStatus.POSTED).exclude(payment_status=InvoicePaymentStatus.PAID)
    submitted = waiting.aggregate(n=Count("id"), amount=Sum("total"))
    owed = approved.aggregate(n=Count("id"), amount=Sum(F("total") - F("amount_paid")))
    start, end = _dates(window, as_of)
    paid = scope.filter(
        JournalLine.objects.filter(
            entry__entity=entity, entry__status__in=LEDGER_STATUSES,
            entry__date__gte=start, entry__date__lte=end,
            account_id__in=sets["claims"], debit__gt=0,
        ),
        "entry__",
    ).aggregate(n=Count("entry_id", distinct=True), amount=Sum("debit"))
    oldest = [
        {
            "id": c.id, "claimant": c.claimant_name or _user_label(c.claimant),
            "title": c.title or c.narration, "days": (as_of - c.claim_date).days, "amount": _m(c.total),
        }
        for c in waiting.select_related("claimant").order_by("claim_date", "id")[:OLDEST_CLAIMS]
    ]
    return {
        "submitted": {"count": submitted["n"], "amount": _m(submitted["amount"])},
        "approved": {"count": owed["n"], "amount": _m(owed["amount"])},
        "paid": {"count": paid["n"], "amount": _m(paid["amount"])},
        "oldest": oldest,
    }


def petty_cash(entity, scope=UNNARROWED) -> dict | None:
    """Each float's cash on hand against what it is topped up to."""
    from .banking_settings import resolve_finance_banking_settings
    from .models import PettyCashFund

    funds = list(
        scope.filter(PettyCashFund.objects.filter(entity=entity, is_active=True))
        .select_related("branch").order_by("name")
    )
    if not funds:
        return None
    threshold = resolve_finance_banking_settings(entity).petty_cash_low_balance_threshold_bps
    return {
        "threshold_pct": round(threshold / 100),
        "funds": [
            {
                "id": f.id, "name": f.name, "branch": f.branch.name if f.branch_id else None,
                "balance": _m(f.current_balance), "float": _m(f.float_amount),
                "low": f.float_amount > 0 and f.current_balance * 10000 < f.float_amount * threshold,
                "last_topped_up": f.last_replenished_at.isoformat() if f.last_replenished_at else None,
            }
            for f in funds
        ],
    }


# --------------------------------------------------------------------------- #
# Compliance and assets                                                       #
# --------------------------------------------------------------------------- #

def tax_owed(entity, as_of) -> dict:
    """Everything owed on open returns, and the next one due."""
    from .models import TaxFiling

    open_filings = (
        TaxFiling.objects.filter(entity=entity)
        .exclude(filing_status__in=["PAID", "CANCELLED"])
        .filter(amount_due__gt=F("amount_paid"))
    )
    total = open_filings.aggregate(owed=Sum(F("amount_due") - F("amount_paid")))["owed"]
    first = open_filings.exclude(due_date=None).select_related("obligation").order_by("due_date").first()
    return {
        "amount": _m(total),
        "next": {
            "name": first.obligation.name, "due_date": first.due_date.isoformat(),
            "days": (first.due_date - as_of).days, "amount": _m(first.amount_due - first.amount_paid),
        } if first else None,
    }


def tax_calendar(entity, as_of) -> list[dict]:
    """Returns due from a month ago to six weeks ahead, nil returns included: they must still be filed."""
    from .models import TaxFiling

    filings = (
        TaxFiling.objects.filter(
            entity=entity, due_date__gte=as_of - datetime.timedelta(days=TAX_LOOKBACK_DAYS),
            due_date__lte=as_of + datetime.timedelta(days=TAX_LOOKAHEAD_DAYS),
        )
        .exclude(filing_status="CANCELLED").select_related("obligation").order_by("due_date", "obligation__name")
    )
    out = []
    for f in filings:
        if f.filing_status == "PAID" or (f.amount_due and f.amount_paid >= f.amount_due):
            state = "paid"
        elif f.filing_status == "FILED":
            state = "filed"
        elif not f.amount_due:
            state = "nil"
        else:
            state = "prepared"
        out.append({
            "id": f.id, "name": f.obligation.name, "code": f.obligation.code,
            "period": f.period_start.strftime("%B %Y"), "due_date": f.due_date.isoformat(),
            "days": (f.due_date - as_of).days, "amount": _m(f.amount_due), "state": state,
        })
    return out


def fixed_assets(entity, window, as_of, scope=UNNARROWED) -> dict | None:
    """What the register is worth, by category, and the window's depreciation."""
    from .constants import AssetCategory
    from .models import DepreciationSchedule, FixedAsset

    assets = scope.filter(
        FixedAsset.objects.filter(entity=entity, asset_status__in=["ACTIVE", "FULLY_DEPRECIATED"]),
    )
    rows = list(
        assets.values("category").annotate(cost=Sum("cost"), dep=Sum("accumulated_depreciation"), n=Count("id"))
    )
    if not rows:
        return None
    start, end = _dates(window, as_of)
    charged = DepreciationSchedule.objects.filter(
        asset__in=assets, is_posted=True, depreciation_date__gte=start, depreciation_date__lte=end,
    ).aggregate(total=Sum("amount"))["total"]
    labels = dict(AssetCategory.choices)
    categories = sorted(
        (
            {
                "key": r["category"], "label": labels.get(r["category"], r["category"]), "count": r["n"],
                "cost": _m(r["cost"]), "net_book_value": _m(r["cost"] - r["dep"]),
                "depreciated_pct": _ratio(r["dep"], r["cost"]),
            }
            for r in rows
        ),
        key=lambda c: -c["net_book_value"]["kobo"],
    )
    return {
        "net_book_value": _m(sum(r["cost"] - r["dep"] for r in rows)),
        "depreciation": _m(charged),
        "categories": categories,
        "fully_depreciated_in_use": assets.filter(asset_status="FULLY_DEPRECIATED").count(),
    }


# --------------------------------------------------------------------------- #
# The view                                                                    #
# --------------------------------------------------------------------------- #

def spend_view(entity, *, reader, window=None, period=None, user=None) -> dict:
    """The Cash, spend & compliance payload for ``entity`` as ``reader`` may see it."""
    from . import dashboard as base

    current = base._current_period(entity, period)
    as_of = current.end_date if period is not None and current is not None else datetime.date.today()
    chosen, windows = resolve_window(entity, as_of, current, window)
    scope = reader.scope
    whole = reader.whole_tenant
    ledger = reader.can("finance.report.view")
    sets = _account_sets(entity) if ledger or reader.can("finance.expenseclaim.view") else None

    cash = cash_movement(entity, chosen, as_of, sets) if whole and ledger else None
    banks = bank_accounts(entity) if whole and reader.can("finance.bankaccount.view") else None
    return {
        "entity": entity.code,
        "books": books_kind(entity),
        "reader_first_name": (getattr(user, "first_name", "") or "").strip() or None,
        "as_of": as_of.isoformat(),
        "narrowed": not whole,
        "window": chosen.payload(),
        "windows": [{"key": w.key, "label": w.label, "name": w.name} for w in windows],
        "runway": (
            runway(entity, as_of, sets, _cash_on(entity, as_of + datetime.timedelta(days=1), sets["cash"]))
            if cash else None
        ),
        "cash_movement": cash,
        "spend": operating_spend(entity, chosen, as_of, sets, scope) if ledger else None,
        "spending": spending(entity, chosen, as_of, sets, scope) if ledger else None,
        "reconciliation": reconciliation(banks) if banks else None,
        "unmatched": (
            {"lines": sum(b["unmatched_lines"] for b in banks),
             "amount": _m(sum(b["unmatched_amount"]["kobo"] for b in banks))}
            if banks else None
        ),
        "budgets": (
            budgets(entity, getattr(current, "fiscal_year", None), as_of, scope)
            if reader.can("finance.budget.view") else None
        ),
        "payroll": payroll(entity, as_of) if whole and reader.can("finance.payrollrun.view") else None,
        "claims": (
            expense_claims(entity, chosen, as_of, sets, scope) if reader.can("finance.expenseclaim.view") else None
        ),
        "petty_cash": petty_cash(entity, scope) if reader.can("finance.pettycash.view") else None,
        "tax_owed": tax_owed(entity, as_of) if whole and reader.can("finance.tax.view") else None,
        "tax_calendar": tax_calendar(entity, as_of) if whole and reader.can("finance.tax.view") else None,
        "assets": fixed_assets(entity, chosen, as_of, scope) if reader.can("finance.fixedasset.view") else None,
    }
