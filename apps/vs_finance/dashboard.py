"""Aggregated **Finance overview** dashboard.

One executive payload computed live from the GL, composing the existing report
services (:mod:`vs_finance.reports`, :mod:`vs_finance.close`) with a few
dashboard-only computations (KPI sparklines/deltas from per-period
:class:`AccountBalance`, the trailing-12-month receivables-vs-collections series,
overdue/vendor-due lists and pending-approval counts).

Everything is entity-scoped. Cross-module reads (procurement payables / vendor
bills / approvals) are best-effort and degrade to empty rather than failing the
whole dashboard, so a procurement hiccup never blanks the finance landing page.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db.models import F, Sum
from django.db.models.functions import TruncMonth

from .constants import (
    AccountType,
    DocumentStatus,
    InvoicePaymentStatus,
    NormalBalance,
    PeriodStatus,
)
from .models import AccountBalance, BankAccount, Customer, FiscalPeriod, Invoice, Payment
from .money import format_naira

SPARK_POINTS = 6          # KPI sparkline length (month-end snapshots incl. current)
TREND_MONTHS = 12         # receivables-vs-collections window
TOP_OVERDUE = 5
VENDOR_DUE_DAYS = 7


def _m(kobo) -> dict:
    """Money envelope matching the rest of the finance API: ``{kobo, naira}``."""
    kobo = int(kobo or 0)
    return {"kobo": kobo, "naira": format_naira(kobo)}


def _user_label(user) -> str:
    """Human label for an actor — the custom User has no ``get_full_name``."""
    if user is None:
        return "system"
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    return name or getattr(user, "email", "") or "system"


def _pct_change(curr: int, prev: int) -> float | None:
    """Signed % change vs the prior point, or ``None`` when there's no base."""
    if not prev:
        return None
    return round((curr - prev) * 100 / abs(prev), 1)


# --------------------------------------------------------------------------- #
# Period window                                                               #
# --------------------------------------------------------------------------- #

def _current_period(entity, period=None):
    """The period the dashboard is based on.

    When the caller pins a ``period`` we use it. Otherwise the *default* is the
    period that **contains today** (so the as-of is the present day), falling back
    to the latest open period, then the latest period.
    """
    qs = FiscalPeriod.objects.filter(entity=entity).select_related("fiscal_year")
    if period is not None:
        return period
    today = datetime.date.today()
    containing = (
        qs.filter(start_date__lte=today, end_date__gte=today)
        .order_by("-fiscal_year__year", "-period_no")
        .first()
    )
    return (
        containing
        or qs.filter(status=PeriodStatus.OPEN).order_by("-fiscal_year__year", "-period_no").first()
        or qs.order_by("-fiscal_year__year", "-period_no").first()
    )


def _fiscal_year_label(current) -> str | None:
    """A span label like ``2025/2026`` (or ``2026`` if the year is single-calendar)."""
    fy = getattr(current, "fiscal_year", None)
    if fy is None:
        return None
    s, e = fy.start_date.year, fy.end_date.year
    return f"{s}/{e}" if s != e else str(fy.year)


def _period_window(entity, current, n=SPARK_POINTS):
    """The up-to-``n`` fiscal periods ending at ``current`` (ascending)."""
    all_p = list(
        FiscalPeriod.objects.filter(entity=entity)
        .select_related("fiscal_year")
        .order_by("fiscal_year__year", "period_no")
    )
    if current is not None:
        idx = next((i for i, p in enumerate(all_p) if p.id == current.id), len(all_p) - 1)
        all_p = all_p[: idx + 1]
    return all_p[-n:]


def _closing_series(account_ids, window_periods, normal) -> list[int]:
    """Closing balance of an account set at each window period-end, signed to ``normal``.

    In this denormalised model each :class:`AccountBalance` row holds only that
    period's *movement* (opening is 0) and rows exist only for periods with activity.
    So the closing balance at period ``p`` is the **cumulative** signed movement over
    every period up to and including ``p`` — including movements that predate the
    sparkline window (e.g. an opening capital injection in period 1).
    """
    if not account_ids or not window_periods:
        return [0 for _ in window_periods]
    sign = 1 if normal == NormalBalance.DEBIT else -1
    rows = (
        AccountBalance.objects.filter(account_id__in=account_ids)
        .values("period__fiscal_year__year", "period__period_no")
        .annotate(dr=Sum("debit_total"), cr=Sum("credit_total"))
    )
    moves: dict[tuple, int] = {}
    for r in rows:
        key = (r["period__fiscal_year__year"], r["period__period_no"])
        moves[key] = moves.get(key, 0) + sign * ((r["dr"] or 0) - (r["cr"] or 0))
    out = []
    for p in window_periods:
        pk = (p.fiscal_year.year, p.period_no)
        out.append(int(sum(v for k, v in moves.items() if k <= pk)))
    return out


def _net_income_series(entity, window_periods) -> list[int]:
    """Cumulative YTD net income at each window period-end.

    Every income/expense leg contributes ``credit − debit`` to net income (income is
    credit-natural so adds; expense is debit-natural so its ``c−d`` is negative and
    subtracts). Accumulated within each period's fiscal year up to its period number —
    so it's true year-to-date even when the window starts mid-year.
    """
    if not window_periods:
        return []
    rows = (
        AccountBalance.objects.filter(
            account__entity=entity,
            account__account_type__in=[AccountType.INCOME, AccountType.EXPENSE],
        )
        .values("period__fiscal_year__year", "period__period_no")
        .annotate(d=Sum("debit_total"), c=Sum("credit_total"))
    )
    net_by: dict[tuple, int] = {}
    for r in rows:
        key = (r["period__fiscal_year__year"], r["period__period_no"])
        net_by[key] = net_by.get(key, 0) + ((r["c"] or 0) - (r["d"] or 0))
    out = []
    for p in window_periods:
        yr, pno = p.fiscal_year.year, p.period_no
        out.append(int(sum(v for (y, n), v in net_by.items() if y == yr and n <= pno)))
    return out


def _kpi(series: list[int]) -> dict:
    curr = series[-1] if series else 0
    prev = series[-2] if len(series) > 1 else 0
    return {"value": _m(curr), "delta_pct": _pct_change(curr, prev), "spark": [int(v) for v in series]}


# --------------------------------------------------------------------------- #
# Cross-module account sets                                                    #
# --------------------------------------------------------------------------- #

def _cash_account_ids(entity) -> set:
    """Cash accounts = the canonical ``1100 Cash & Bank`` GL account plus any GL
    account a :class:`BankAccount` maps to — the same definition the cash-flow
    statement uses, so the dashboard's cash position reconciles to it. (Many
    entities hold cash directly in ``1100`` with no operational BankAccount row.)
    """
    from .constants import CASH_BANK_CODE
    from .models import Account

    ids = set(
        Account.objects.filter(entity=entity, code=CASH_BANK_CODE).values_list("id", flat=True)
    )
    ids |= set(
        BankAccount.objects.filter(entity=entity)
        .exclude(gl_account=None)
        .values_list("gl_account_id", flat=True)
    )
    return ids


def _payable_account_ids(entity) -> set:
    try:
        from vs_procurement.models import Vendor

        return set(
            Vendor.objects.filter(entity=entity)
            .exclude(payable_account=None)
            .values_list("payable_account_id", flat=True)
        )
    except Exception:  # pragma: no cover - procurement optional
        return set()


# --------------------------------------------------------------------------- #
# Blocks                                                                       #
# --------------------------------------------------------------------------- #

def _revenue_vs_budget(entity, fiscal_year) -> dict:
    from .reports import budget_vs_actual, income_statement

    pnl = income_statement(entity)  # YTD, whole open year
    rev_actual, exp_actual = pnl.total_income, pnl.total_expense

    budget = None
    if fiscal_year is not None:
        from .models import Budget

        budget = (
            Budget.objects.filter(entity=entity, fiscal_year=fiscal_year)
            .order_by("-approved_at", "-id")
            .first()
        )
    rev_plan = exp_plan = 0
    if budget is not None:
        rep = budget_vs_actual(budget)
        for r in rep.rows:
            if r.account_type == AccountType.INCOME:
                rev_plan += r.budget
            elif r.account_type == AccountType.EXPENSE:
                exp_plan += r.budget

    def line(actual, plan):
        pct = round(actual * 100 / plan) if plan else None
        return {"actual": _m(actual), "plan": _m(plan), "pct_of_plan": pct}

    net_actual = rev_actual - exp_actual
    net_plan = rev_plan - exp_plan
    return {
        "has_budget": budget is not None,
        "budget_name": getattr(budget, "name", None),
        "revenue": line(rev_actual, rev_plan),
        "expense": line(exp_actual, exp_plan),
        "net": {"actual": _m(net_actual), "delta_pct": _pct_change(net_actual, net_plan)},
    }


def _ar_aging_block(entity, as_of) -> dict:
    from .reports import ar_aging

    rep = ar_aging(entity, as_of=as_of)
    total = rep.total_net or 0
    buckets = []
    for key, amount in rep.bucket_totals.items():
        amount = int(amount or 0)
        pct = round(amount * 100 / total) if total else 0
        buckets.append({"key": key, "pct": pct, "amount": _m(amount)})
    return {"buckets": buckets, "total": _m(total)}


def _trend(entity, anchor) -> dict:
    """Trailing-12-month receivable-issued vs collected ending at ``anchor``'s month."""
    first = anchor.replace(day=1)
    # step back 11 months for a 12-point window
    y, mo = first.year, first.month - (TREND_MONTHS - 1)
    while mo <= 0:
        mo += 12
        y -= 1
    start = datetime.date(y, mo, 1)

    issued = {
        r["m"]: int(r["s"] or 0)
        for r in Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=start
        )
        .annotate(m=TruncMonth("invoice_date"))
        .values("m")
        .annotate(s=Sum("total"))
    }
    collected = {
        r["m"]: int(r["s"] or 0)
        for r in Payment.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start
        )
        .annotate(m=TruncMonth("payment_date"))
        .values("m")
        .annotate(s=Sum("amount"))
    }
    labels, iss, col = [], [], []
    cur = start
    for _ in range(TREND_MONTHS):
        key = datetime.date(cur.year, cur.month, 1)
        labels.append(cur.strftime("%b %y"))
        iss.append(issued.get(key, 0))
        col.append(collected.get(key, 0))
        cur = datetime.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return {"labels": labels, "issued": iss, "collected": col}


def _top_overdue(entity, as_of) -> list[dict]:
    today = as_of or datetime.date.today()
    qs = (
        Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=today
        )
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
        .filter(bal__gt=0)
        .select_related("customer")
        .order_by("-bal")[:TOP_OVERDUE]
    )
    return [
        {
            "customer": i.customer.name,
            "customer_code": i.customer.code,
            "reference": i.document_number,
            "amount": _m(i.bal),
            "days_overdue": (today - i.due_date).days,
        }
        for i in qs
    ]


def _vendor_due(entity) -> list[dict]:
    try:
        from vs_procurement.models import VendorInvoice

        today = datetime.date.today()
        end = today + datetime.timedelta(days=VENDOR_DUE_DAYS)
        qs = (
            VendorInvoice.objects.filter(
                entity=entity,
                status=DocumentStatus.POSTED,
                due_date__gte=today,
                due_date__lte=end,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .annotate(bal=F("total") - F("amount_paid"))
            .filter(bal__gt=0)
            .select_related("vendor")
            .order_by("due_date")[:TOP_OVERDUE]
        )
        return [
            {
                "vendor": v.vendor.name,
                "reference": v.document_number,
                "due_date": v.due_date.isoformat(),
                "amount": _m(v.bal),
                "days_until": (v.due_date - today).days,
            }
            for v in qs
        ]
    except Exception:  # pragma: no cover - procurement optional
        return []


def _approvals(entity) -> dict:
    """Pending spend-approvals, counted from each procurement doc's ``approval_state``.

    Entity-scoped and read straight off the document overlay (no cross-app workflow
    join), so it's exact and can't leak another entity's counts.
    """
    items = []
    try:
        from vs_procurement import models as pm
        from vs_procurement.constants import ProcApprovalState

        pending = ProcApprovalState.PENDING
        specs = [
            ("PurchaseRequisition", "Purchase requisitions"),
            ("PurchaseOrder", "Purchase orders"),
            ("VendorInvoice", "Vendor invoices"),
        ]
        for model_name, label in specs:
            model = getattr(pm, model_name, None)
            if model is None:
                continue
            count = model.objects.filter(entity=entity, approval_state=pending).count()
            items.append({"label": label, "count": count})
    except Exception:  # pragma: no cover - procurement optional
        pass
    return {"items": items, "total": sum(i["count"] for i in items)}


def _close_progress(entity, period) -> dict | None:
    if period is None:
        return None
    from .close import close_checklist

    try:
        cl = close_checklist(entity, period)
    except Exception:  # pragma: no cover - defensive
        return None
    checks = [{"name": i.name, "passed": bool(i.passed), "blocking": bool(i.blocking)} for i in cl.items]
    return {
        "period": period.name,
        "done": sum(1 for c in checks if c["passed"]),
        "total": len(checks),
        "checks": checks,
    }


def _recent_journals(entity, limit=5) -> list[dict]:
    from .models import JournalEntry

    qs = (
        JournalEntry.objects.filter(entity=entity)
        .select_related("created_by")
        .order_by("-date", "-id")[:limit]
    )
    out = []
    for j in qs:
        dr, _cr = j.totals()
        out.append(
            {
                "document_number": j.document_number,
                "date": j.date.isoformat(),
                "source": getattr(j, "source", "") or "Manual",
                "narration": getattr(j, "narration", "") or "",
                "amount": _m(dr),
                "status": j.status,
                "created_by": _user_label(getattr(j, "created_by", None)),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class FinanceDashboard:
    payload: dict = field(default_factory=dict)


def finance_dashboard(entity, *, period=None) -> dict:
    """Assemble the whole Finance-overview payload for ``entity``."""
    current = _current_period(entity, period)
    # Default as-of is the present day; pinning a period moves it to that period's end.
    if period is not None and current is not None:
        as_of = current.end_date
    else:
        as_of = datetime.date.today()
    periods = _period_window(entity, current)

    cash = _closing_series(_cash_account_ids(entity), periods, NormalBalance.DEBIT)
    ar = _closing_series(
        set(
            Customer.objects.filter(entity=entity)
            .exclude(receivable_account=None)
            .values_list("receivable_account_id", flat=True)
        ),
        periods,
        NormalBalance.DEBIT,
    )
    ap = _closing_series(_payable_account_ids(entity), periods, NormalBalance.CREDIT)
    ni = _net_income_series(entity, periods)

    return {
        "entity": entity.code,
        "fiscal_year": _fiscal_year_label(current),
        "period": getattr(current, "name", None),
        "as_of": as_of.isoformat(),
        "kpis": {
            "cash_position": _kpi(cash),
            "receivables": _kpi(ar),
            "payables": _kpi(ap),
            "net_income_ytd": _kpi(ni),
        },
        "revenue_vs_budget": _revenue_vs_budget(entity, getattr(current, "fiscal_year", None)),
        "ar_aging": _ar_aging_block(entity, as_of),
        "trend": _trend(entity, as_of),
        "top_overdue": _top_overdue(entity, as_of),
        "vendor_due": _vendor_due(entity),
        "approvals": _approvals(entity),
        "close_progress": _close_progress(entity, current),
        "recent_journals": _recent_journals(entity),
    }
