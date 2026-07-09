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
from __future__ import annotations  # Defer annotation evaluation during app import.

import datetime  # Date arithmetic for period windows and trends.
from dataclasses import dataclass, field  # Lightweight dashboard payload container.

from django.db.models import F, Sum  # Database expressions and aggregation helpers.
from django.db.models.functions import TruncMonth  # Month bucketing for trend charts.

from .constants import (  # Import project symbols used by this module.
    AccountType,  # Account classification enum.
    DocumentStatus,  # Finance document lifecycle statuses.
    InvoicePaymentStatus,  # Invoice paid/partial/unpaid statuses.
    NormalBalance,  # Debit/credit normal balance enum.
    PeriodStatus,  # Fiscal period lifecycle statuses.
)  # Close the grouped expression.
from .models import AccountBalance, BankAccount, Customer, FiscalPeriod, Invoice, Payment  # Dashboard query models.
from .money import format_naira  # Formats integer-kobo amounts for API display.

SPARK_POINTS = 6          # KPI sparkline length (month-end snapshots incl. current)
TREND_MONTHS = 12         # receivables-vs-collections window
TOP_OVERDUE = 5  # Number of overdue/vendor rows to surface.
VENDOR_DUE_DAYS = 7  # Forward-looking vendor due window.


def _m(kobo) -> dict:  # Wrap integer kobo in standard money response shape.
    """Money envelope matching the rest of the finance API: ``{kobo, naira}``."""
    kobo = int(kobo or 0)  # Normalize missing values to zero.
    return {"kobo": kobo, "naira": format_naira(kobo)}  # Include raw kobo and formatted naira.


def _user_label(user) -> str:  # Build a display label for a user or system actor.
    """Human label for an actor — the custom User has no ``get_full_name``."""
    if user is None:  # System-generated rows have no user.
        return "system"
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()  # Build full name defensively.
    return name or getattr(user, "email", "") or "system"  # Prefer name, fallback to email/system.


def _pct_change(curr: int, prev: int) -> float | None:  # Compute signed percentage movement.
    """Signed % change vs the prior point, or ``None`` when there's no base."""
    if not prev:  # Avoid divide-by-zero and undefined base.
        return None  # Return the computed module result.
    return round((curr - prev) * 100 / abs(prev), 1)  # Return one-decimal percent delta.


# --------------------------------------------------------------------------- #
# Period window                                                               #
# --------------------------------------------------------------------------- #

def _current_period(entity, period=None):  # Resolve dashboard anchor period.
    """The period the dashboard is based on.

    When the caller pins a ``period`` we use it. Otherwise the *default* is the
    period that **contains today** (so the as-of is the present day), falling back
    to the latest open period, then the latest period.
    """
    qs = FiscalPeriod.objects.filter(entity=entity).select_related("fiscal_year")  # Entity periods with fiscal year loaded.
    if period is not None:  # Caller explicitly pinned a period.
        return period  # Return the computed module result.
    today = datetime.date.today()  # Default dashboard date.
    containing = (  # Prefer the period containing today.
        qs.filter(start_date__lte=today, end_date__gte=today)  # Date within period boundaries.
        .order_by("-fiscal_year__year", "-period_no")  # Stable latest match.
        .first()  # Return one period or None.
    )  # Close the grouped expression.
    return (  # Fallback chain when today has no period.
        containing  # Current calendar period.
        or qs.filter(status=PeriodStatus.OPEN).order_by("-fiscal_year__year", "-period_no").first()  # Latest open period.
        or qs.order_by("-fiscal_year__year", "-period_no").first()  # Latest period of any status.
    )  # Close the grouped expression.


def _fiscal_year_label(current) -> str | None:  # Format fiscal year span for dashboard header.
    """A span label like ``2025/2026`` (or ``2026`` if the year is single-calendar)."""
    fy = getattr(current, "fiscal_year", None)  # Current period may be absent.
    if fy is None:  # No fiscal year to label.
        return None  # Return the computed module result.
    s, e = fy.start_date.year, fy.end_date.year  # Fiscal year start/end calendar years.
    return f"{s}/{e}" if s != e else str(fy.year)  # Use span only when crossing calendar years.


def _period_window(entity, current, n=SPARK_POINTS):  # Build trailing fiscal-period window.
    """The up-to-``n`` fiscal periods ending at ``current`` (ascending)."""
    all_p = list(  # Load all entity periods in chronological order.
        FiscalPeriod.objects.filter(entity=entity)  # Scope to entity.
        .select_related("fiscal_year")  # Load fiscal year for sorting and labels.
        .order_by("fiscal_year__year", "period_no")  # Chronological order.
    )  # Close the grouped expression.
    if current is not None:  # Trim to periods ending at the current anchor.
        idx = next((i for i, p in enumerate(all_p) if p.id == current.id), len(all_p) - 1)  # Find anchor index.
        all_p = all_p[: idx + 1]  # Keep periods up to anchor.
    return all_p[-n:]  # Return final n periods.


def _closing_series(account_ids, window_periods, normal) -> list[int]:  # Build cumulative closing-balance series.
    """Closing balance of an account set at each window period-end, signed to ``normal``.

    In this denormalised model each :class:`AccountBalance` row holds only that
    period's *movement* (opening is 0) and rows exist only for periods with activity.
    So the closing balance at period ``p`` is the **cumulative** signed movement over
    every period up to and including ``p`` — including movements that predate the
    sparkline window (e.g. an opening capital injection in period 1).
    """
    if not account_ids or not window_periods:  # No accounts or periods means zero series.
        return [0 for _ in window_periods]  # Return the computed module result.
    sign = 1 if normal == NormalBalance.DEBIT else -1  # Convert movements to natural-balance sign.
    rows = (  # Aggregate account movements by fiscal period.
        AccountBalance.objects.filter(account_id__in=account_ids)  # Account set to chart.
        .values("period__fiscal_year__year", "period__period_no")  # Group by period identity.
        .annotate(dr=Sum("debit_total"), cr=Sum("credit_total"))  # Sum period debit/credit movements.
    )  # Close the grouped expression.
    moves: dict[tuple, int] = {}  # Movement by fiscal period key.
    for r in rows:  # Convert aggregate rows to signed movements.
        key = (r["period__fiscal_year__year"], r["period__period_no"])  # Comparable period key.
        moves[key] = moves.get(key, 0) + sign * ((r["dr"] or 0) - (r["cr"] or 0))  # Natural signed movement.
    out = []  # Closing balances at each window point.
    for p in window_periods:  # Compute cumulative balance through each period.
        pk = (p.fiscal_year.year, p.period_no)  # Current period key.
        out.append(int(sum(v for k, v in moves.items() if k <= pk)))  # Sum all prior/current movement.
    return out  # Return closing series.


def _net_income_series(entity, window_periods) -> list[int]:  # Build YTD net-income series.
    """Cumulative YTD net income at each window period-end.

    Every income/expense leg contributes ``credit − debit`` to net income (income is
    credit-natural so adds; expense is debit-natural so its ``c−d`` is negative and
    subtracts). Accumulated within each period's fiscal year up to its period number —
    so it's true year-to-date even when the window starts mid-year.
    """
    if not window_periods:  # No period window means no series.
        return []  # Return the computed module result.
    rows = (  # Aggregate income/expense movements by period.
        AccountBalance.objects.filter(  # Restrict balances to P&L accounts.
            account__entity=entity,  # Scope to entity.
            account__account_type__in=[AccountType.INCOME, AccountType.EXPENSE],  # Income and expenses only.
        )  # Close the grouped expression.
        .values("period__fiscal_year__year", "period__period_no")  # Group by fiscal period.
        .annotate(d=Sum("debit_total"), c=Sum("credit_total"))  # Sum debit/credit movements.
    )  # Close the grouped expression.
    net_by: dict[tuple, int] = {}  # Net income movement by period key.
    for r in rows:  # Convert rows to net P&L movement.
        key = (r["period__fiscal_year__year"], r["period__period_no"])  # Comparable period key.
        net_by[key] = net_by.get(key, 0) + ((r["c"] or 0) - (r["d"] or 0))  # Income credits less expense debits.
    out = []  # YTD series values.
    for p in window_periods:  # Build value for each period.
        yr, pno = p.fiscal_year.year, p.period_no  # Current fiscal year and period number.
        out.append(int(sum(v for (y, n), v in net_by.items() if y == yr and n <= pno)))  # Sum YTD within fiscal year.
    return out  # Return YTD net-income series.


def _kpi(series: list[int]) -> dict:  # Convert a raw series to dashboard KPI shape.
    curr = series[-1] if series else 0  # Current value is final point.
    prev = series[-2] if len(series) > 1 else 0  # Prior value drives delta.
    return {"value": _m(curr), "delta_pct": _pct_change(curr, prev), "spark": [int(v) for v in series]}  # KPI payload.


# --------------------------------------------------------------------------- #
# Cross-module account sets                                                    #
# --------------------------------------------------------------------------- #

def _cash_account_ids(entity) -> set:  # Resolve GL account ids that count as cash.
    """Cash accounts = the canonical ``1100 Cash & Bank`` GL account plus any GL
    account a :class:`BankAccount` maps to — the same definition the cash-flow
    statement uses, so the dashboard's cash position reconciles to it. (Many
    entities hold cash directly in ``1100`` with no operational BankAccount row.)
    """
    from .constants import CASH_BANK_CODE  # Canonical cash/bank account code.
    from .models import Account  # Account model for direct cash account lookup.

    ids = set(  # Start with canonical cash account when present.
        Account.objects.filter(entity=entity, code=CASH_BANK_CODE).values_list("id", flat=True)  # Cash & Bank account id.
    )  # Close the grouped expression.
    ids |= set(  # Include operational bank accounts mapped to GL accounts.
        BankAccount.objects.filter(entity=entity)  # Entity bank accounts.
        .exclude(gl_account=None)  # Ignore incomplete bank accounts.
        .values_list("gl_account_id", flat=True)  # Bank GL account ids.
    )  # Close the grouped expression.
    return ids  # Return cash-equivalent account ids.


def _payable_account_ids(entity) -> set:  # Resolve vendor payable account ids best-effort.
    try:  # Procurement may be unavailable or misconfigured.
        from vs_procurement.models import Vendor  # Vendor model with payable account.

        return set(  # Vendor payable control account ids.
            Vendor.objects.filter(entity=entity)  # Vendors in this entity.
            .exclude(payable_account=None)  # Ignore vendors without payable account.
            .values_list("payable_account_id", flat=True)  # Payable account ids.
        )  # Close the grouped expression.
    except Exception:  # pragma: no cover - procurement optional
        return set()  # Degrade to no payables instead of breaking dashboard.


# --------------------------------------------------------------------------- #
# Blocks                                                                       #
# --------------------------------------------------------------------------- #

def _revenue_vs_budget(entity, fiscal_year) -> dict:  # Build revenue/expense actual-vs-plan block.
    from .reports import budget_vs_actual, income_statement  # Existing report services.

    pnl = income_statement(entity)  # YTD, whole open year
    rev_actual, exp_actual = pnl.total_income, pnl.total_expense  # Actual P&L totals.

    budget = None  # Optional approved/latest budget.
    if fiscal_year is not None:  # Budget lookup requires a fiscal year.
        from .models import Budget  # Local import avoids model import cycles.

        budget = (  # Choose latest budget for the fiscal year.
            Budget.objects.filter(entity=entity, fiscal_year=fiscal_year)  # Entity/year budgets.
            .order_by("-approved_at", "-id")  # Prefer latest approved/latest created.
            .first()  # Return one budget or None.
        )  # Close the grouped expression.
    rev_plan = exp_plan = 0  # Budget totals default to zero.
    if budget is not None:  # Compute plan totals when a budget exists.
        rep = budget_vs_actual(budget)  # Reuse budget-vs-actual report rows.
        for r in rep.rows:  # Sum budget by P&L account type.
            if r.account_type == AccountType.INCOME:  # Income budget row.
                rev_plan += r.budget  # Add revenue plan.
            elif r.account_type == AccountType.EXPENSE:  # Expense budget row.
                exp_plan += r.budget  # Add expense plan.

    def line(actual, plan):  # Shape one actual-vs-plan line.
        pct = round(actual * 100 / plan) if plan else None  # Percentage of plan when plan exists.
        return {"actual": _m(actual), "plan": _m(plan), "pct_of_plan": pct}  # Line payload.

    net_actual = rev_actual - exp_actual  # Actual net income.
    net_plan = rev_plan - exp_plan  # Planned net income.
    return {  # Return budget block.
        "has_budget": budget is not None,  # UI flag.
        "budget_name": getattr(budget, "name", None),  # Budget display name.
        "revenue": line(rev_actual, rev_plan),  # Revenue actual vs plan.
        "expense": line(exp_actual, exp_plan),  # Expense actual vs plan.
        "net": {"actual": _m(net_actual), "delta_pct": _pct_change(net_actual, net_plan)},  # Net actual and plan delta.
    }  # Close the grouped expression.


def _ar_aging_block(entity, as_of) -> dict:  # Build AR aging chart block.
    from .reports import ar_aging  # Existing AR aging report service.

    rep = ar_aging(entity, as_of=as_of)  # Compute aging report as of dashboard date.
    total = rep.total_net or 0  # Total outstanding AR.
    buckets = []  # Aging bucket payload rows.
    for key, amount in rep.bucket_totals.items():  # Convert bucket totals to display shape.
        amount = int(amount or 0)  # Normalize missing amount.
        pct = round(amount * 100 / total) if total else 0  # Bucket share of total.
        buckets.append({"key": key, "pct": pct, "amount": _m(amount)})  # Append bucket payload.
    return {"buckets": buckets, "total": _m(total)}  # Return aging block.


def _trend(entity, anchor) -> dict:  # Build trailing receivables-issued vs collections trend.
    """Trailing-12-month receivable-issued vs collected ending at ``anchor``'s month."""
    first = anchor.replace(day=1)  # Anchor to first day of dashboard month.
    # step back 11 months for a 12-point window  # Inclusive current month.
    y, mo = first.year, first.month - (TREND_MONTHS - 1)  # Raw starting month.
    while mo <= 0:  # Roll back across calendar years.
        mo += 12  # Normalize month into 1-12.
        y -= 1  # Move to previous year.
    start = datetime.date(y, mo, 1)  # First month in trend window.

    issued = {  # Posted invoice totals by month.
        r["m"]: int(r["s"] or 0)  # Month bucket -> total issued.
        for r in Invoice.objects.filter(  # Query posted invoices in window.
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=start  # Entity, posted, after start.
        )  # Close the grouped expression.
        .annotate(m=TruncMonth("invoice_date"))  # Bucket invoice date by month.
        .values("m")  # Group by month.
        .annotate(s=Sum("total"))  # Sum invoice totals.
    }  # Close the grouped expression.
    collected = {  # Posted payment totals by month.
        r["m"]: int(r["s"] or 0)  # Month bucket -> total collected.
        for r in Payment.objects.filter(  # Query posted payments in window.
            entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start  # Entity, posted, after start.
        )  # Close the grouped expression.
        .annotate(m=TruncMonth("payment_date"))  # Bucket payment date by month.
        .values("m")  # Group by month.
        .annotate(s=Sum("amount"))  # Sum payment amounts.
    }  # Close the grouped expression.
    labels, iss, col = [], [], []  # Chart labels and two series.
    cur = start  # Current month cursor.
    for _ in range(TREND_MONTHS):  # Build fixed-length trend arrays.
        key = datetime.date(cur.year, cur.month, 1)  # Month key matching TruncMonth output date.
        labels.append(cur.strftime("%b %y"))  # Human month label.
        iss.append(issued.get(key, 0))  # Issued amount or zero.
        col.append(collected.get(key, 0))  # Collected amount or zero.
        cur = datetime.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)  # Advance one month.
    return {"labels": labels, "issued": iss, "collected": col}  # Return trend block.


def _top_overdue(entity, as_of) -> list[dict]:  # Return top overdue customer invoices.
    today = as_of or datetime.date.today()  # Dashboard aging date.
    qs = (  # Query open overdue invoices.
        Invoice.objects.filter(  # Posted invoices due before as-of.
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=today  # Entity, posted, overdue.
        )  # Close the grouped expression.
        .exclude(payment_status=InvoicePaymentStatus.PAID)  # Exclude fully paid invoices.
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))  # Compute outstanding balance.
        .filter(bal__gt=0)  # Keep invoices with actual balance.
        .select_related("customer")  # Load customer for row payload.
        .order_by("-bal")[:TOP_OVERDUE]  # Largest overdue balances first.
    )  # Close the grouped expression.
    return [  # Shape overdue rows.
        {  # Continue the structured value.
            "customer": i.customer.name,  # Customer name.
            "customer_code": i.customer.code,  # Customer code.
            "reference": i.document_number,  # Invoice number.
            "amount": _m(i.bal),  # Outstanding amount.
            "days_overdue": (today - i.due_date).days,  # Age past due.
        }  # Close the grouped expression.
        for i in qs  # Iterate top overdue invoices.
    ]  # Close the grouped expression.


def _vendor_due(entity) -> list[dict]:  # Return upcoming vendor bills best-effort.
    try:  # Procurement app is optional for dashboard resilience.
        from vs_procurement.models import VendorInvoice  # Vendor bill model.

        today = datetime.date.today()  # Start of due window.
        end = today + datetime.timedelta(days=VENDOR_DUE_DAYS)  # End of due window.
        qs = (  # Query upcoming open vendor bills.
            VendorInvoice.objects.filter(  # Posted bills due within window.
                entity=entity,  # Scope to entity.
                status=DocumentStatus.POSTED,  # Posted vendor invoices only.
                due_date__gte=today,  # Not already overdue.
                due_date__lte=end,  # Due within configured window.
            )  # Close the grouped expression.
            .exclude(payment_status=InvoicePaymentStatus.PAID)  # Exclude paid bills.
            .annotate(bal=F("total") - F("amount_paid"))  # Compute balance.
            .filter(bal__gt=0)  # Keep bills with balance.
            .select_related("vendor")  # Load vendor for row payload.
            .order_by("due_date")[:TOP_OVERDUE]  # Soonest due first.
        )  # Close the grouped expression.
        return [  # Shape vendor due rows.
            {  # Continue the structured value.
                "vendor": v.vendor.name,  # Vendor name.
                "reference": v.document_number,  # Bill number.
                "due_date": v.due_date.isoformat(),  # ISO due date.
                "amount": _m(v.bal),  # Outstanding amount.
                "days_until": (v.due_date - today).days,  # Days until due.
            }  # Close the grouped expression.
            for v in qs  # Iterate vendor bills.
        ]  # Close the grouped expression.
    except Exception:  # pragma: no cover - procurement optional
        return []  # Degrade to empty vendor list.


def _approvals(entity) -> dict:  # Count pending procurement approval overlays.
    """Pending spend-approvals, counted from each procurement doc's ``approval_state``.

    Entity-scoped and read straight off the document overlay (no cross-app workflow
    join), so it's exact and can't leak another entity's counts.
    """
    items = []  # Approval count rows.
    try:  # Procurement app is optional for dashboard resilience.
        from vs_procurement import models as pm  # Procurement model module.
        from vs_procurement.constants import ProcApprovalState  # Procurement approval state enum.

        pending = ProcApprovalState.PENDING  # Pending approval state.
        specs = [  # Procurement documents shown on dashboard.
            ("PurchaseRequisition", "Purchase requisitions"),  # Requisition count spec.
            ("PurchaseOrder", "Purchase orders"),  # PO count spec.
            ("VendorInvoice", "Vendor invoices"),  # Vendor invoice count spec.
        ]  # Close the grouped expression.
        for model_name, label in specs:  # Count each procurement document type.
            model = getattr(pm, model_name, None)  # Resolve model defensively.
            if model is None:  # Skip unavailable model.
                continue  # Skip to the next loop iteration.
            count = model.objects.filter(entity=entity, approval_state=pending).count()  # Entity-scoped pending count.
            items.append({"label": label, "count": count})  # Add count row.
    except Exception:  # pragma: no cover - procurement optional
        pass  # Degrade to empty approvals list.
    return {"items": items, "total": sum(i["count"] for i in items)}  # Return counts and total.


def _close_progress(entity, period) -> dict | None:  # Summarize period close checklist status.
    if period is None:  # No period means no close checklist.
        return None  # Return the computed module result.
    from .close import close_checklist  # Existing period-close checklist service.

    try:  # Checklist can fail on configuration issues; dashboard should degrade.
        cl = close_checklist(entity, period)  # Run close checks.
    except Exception:  # pragma: no cover - defensive
        return None  # Hide close progress instead of failing dashboard.
    checks = [{"name": i.name, "passed": bool(i.passed), "blocking": bool(i.blocking)} for i in cl.items]  # Shape checklist rows.
    return {  # Return checklist summary.
        "period": period.name,  # Period display name.
        "done": sum(1 for c in checks if c["passed"]),  # Passed check count.
        "total": len(checks),  # Total check count.
        "checks": checks,  # Per-check rows.
    }  # Close the grouped expression.


def _recent_journals(entity, limit=5) -> list[dict]:  # Return recent journal activity rows.
    from .models import JournalEntry  # Local import avoids model import cycles.

    qs = (  # Recent journals for entity.
        JournalEntry.objects.filter(entity=entity)  # Scope to entity.
        .select_related("created_by")  # Load creator for display label.
        .order_by("-date", "-id")[:limit]  # Most recent first.
    )  # Close the grouped expression.
    out = []  # Recent journal payload rows.
    for j in qs:  # Shape each journal row.
        dr, _cr = j.totals()  # Journal amount from debit side.
        out.append(  # Append dashboard row.
            {  # Continue the structured value.
                "document_number": j.document_number,  # Journal number.
                "date": j.date.isoformat(),  # ISO journal date.
                "source": getattr(j, "source", "") or "Manual",  # Journal source label.
                "narration": getattr(j, "narration", "") or "",  # Journal narration.
                "amount": _m(dr),  # Journal amount.
                "status": j.status,  # Journal status.
                "created_by": _user_label(getattr(j, "created_by", None)),  # Creator label.
            }  # Close the grouped expression.
        )  # Close the grouped expression.
    return out  # Return recent journals.


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

@dataclass  # Apply the decorator to this callable.
class FinanceDashboard:  # Thin typed wrapper for dashboard payloads.
    payload: dict = field(default_factory=dict)  # Dashboard response payload.


def finance_dashboard(entity, *, period=None) -> dict:  # Assemble complete finance dashboard payload.
    """Assemble the whole Finance-overview payload for ``entity``."""
    current = _current_period(entity, period)  # Resolve anchor period.
    # Default as-of is the present day; pinning a period moves it to that period's end.  # Makes historical dashboards deterministic.
    if period is not None and current is not None:  # Caller pinned a specific period.
        as_of = current.end_date  # Use period end as dashboard date.
    else:  # Live dashboard uses today.
        as_of = datetime.date.today()  # Current date.
    periods = _period_window(entity, current)  # KPI sparkline period window.

    cash = _closing_series(_cash_account_ids(entity), periods, NormalBalance.DEBIT)  # Cash KPI series.
    ar = _closing_series(  # Receivables KPI series.
        set(  # Customer AR account ids.
            Customer.objects.filter(entity=entity)  # Customers in entity.
            .exclude(receivable_account=None)  # Customers with AR accounts.
            .values_list("receivable_account_id", flat=True)  # AR account ids.
        ),  # Close the grouped value.
        periods,  # Sparkline periods.
        NormalBalance.DEBIT,  # AR is debit-natural.
    )  # Close the grouped expression.
    ap = _closing_series(_payable_account_ids(entity), periods, NormalBalance.CREDIT)  # Payables KPI series.
    ni = _net_income_series(entity, periods)  # Net income YTD KPI series.

    return {  # Complete dashboard payload.
        "entity": entity.code,  # Entity code.
        "fiscal_year": _fiscal_year_label(current),  # Fiscal year label.
        "period": getattr(current, "name", None),  # Current period name.
        "as_of": as_of.isoformat(),  # Dashboard as-of date.
        "kpis": {  # Executive KPI cards.
            "cash_position": _kpi(cash),  # Cash card.
            "receivables": _kpi(ar),  # Receivables card.
            "payables": _kpi(ap),  # Payables card.
            "net_income_ytd": _kpi(ni),  # Net income card.
        },  # Close the grouped value.
        "revenue_vs_budget": _revenue_vs_budget(entity, getattr(current, "fiscal_year", None)),  # Actual vs budget block.
        "ar_aging": _ar_aging_block(entity, as_of),  # AR aging block.
        "trend": _trend(entity, as_of),  # Receivables vs collections trend.
        "top_overdue": _top_overdue(entity, as_of),  # Largest overdue invoices.
        "vendor_due": _vendor_due(entity),  # Upcoming vendor bills.
        "approvals": _approvals(entity),  # Pending procurement approvals.
        "close_progress": _close_progress(entity, current),  # Period-close checklist progress.
        "recent_journals": _recent_journals(entity),  # Recent journal activity.
    }  # Close the grouped expression.
