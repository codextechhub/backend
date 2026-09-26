"""Aggregated **Finance overview** dashboard.

One executive payload computed live from the GL, composing the existing report
services (:mod:`vs_finance.reports`, :mod:`vs_finance.close`) with a few
dashboard-only computations (KPI sparklines/deltas from per-period
:class:`AccountBalance`, the trailing-12-month receivables-vs-collections series,
overdue/vendor-due lists and pending-approval counts).

Everything is entity-scoped. Cross-module reads (procurement payables / vendor
bills / approvals) are best-effort and degrade to empty rather than failing the
whole dashboard, so a procurement hiccup never blanks the finance landing page.

The dashboard opens to anyone working in finance, and each block is computed
only for a reader who holds the key behind it (see :class:`DashboardReader`). A
block the reader may not see is ``None`` in the payload, not an empty value, so
nothing about it leaves the server and the screen can leave the card out rather
than draw an empty one.

Two kinds of figure answer the branch question differently. Figures read off the
documents (invoices, receipts, vendor bills, approvals, journals) are narrowed to
the reader's branches, so a bursar posted to one branch sees that branch and the
school-wide rows, the same rows their lists show. Figures read off the general
ledger balances (cash, payables, net income, revenue against budget, the period
close) cannot be split by branch at all, so they are sent only to a reader whose
reach is the whole school.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db.models import F, Sum
from django.db.models.functions import TruncMonth

from vs_rbac.scoping import UNNARROWED, BranchScope

from .constants import (
    AccountType,
    DocumentStatus,
    InvoicePaymentStatus,
    NormalBalance,
    PeriodStatus,
)
from .models import AccountBalance, BankAccount, Customer, FiscalPeriod, Invoice, Payment
from .money import format_naira
from .posting import fiscal_calendar_runway

SPARK_POINTS = 6          # KPI sparkline length (month-end snapshots incl. current)
TREND_MONTHS = 12         # receivables-vs-collections window
TOP_OVERDUE = 5  # Number of overdue/vendor rows to surface.
VENDOR_DUE_DAYS = 7  # Forward-looking vendor due window.


@dataclass(frozen=True)
class DashboardReader:
    """Who is reading a dashboard: which keys they hold and whose rows they see.

    ``keys`` is ``None`` for a reader who holds every key, which is what callers
    without a request get by default (tests, internal reports). ``scope`` is the
    finance reading of a blank branch (shared rows included); ``procurement_scope``
    is procurement's (a blank branch is the institution, which a branch-pinned
    reader is not in). Both come from the one helper every list uses, so a
    dashboard figure and the list behind it cannot disagree.
    """

    keys: frozenset | None = None
    scope: BranchScope = UNNARROWED
    procurement_scope: BranchScope = UNNARROWED

    def can(self, *keys: str) -> bool:
        """True when the reader holds any one of ``keys``."""
        return self.keys is None or any(key in self.keys for key in keys)

    @property
    def whole_tenant(self) -> bool:
        """True when nothing narrows the reader to particular branches."""
        return not self.scope.is_narrowed

    @classmethod
    def for_user(cls, user, tenant) -> "DashboardReader":
        """The reader behind a request's effective user."""
        from vs_rbac.evaluator import get_effective_permissions
        from vs_rbac.permissions import is_vision_super_admin
        from vs_rbac.scoping import branch_scope_for_user

        keys = None if is_vision_super_admin(user) else frozenset(
            get_effective_permissions(user, tenant=tenant))
        return cls(
            keys=keys,
            scope=branch_scope_for_user(user, include_shared=True, tenant=tenant),
            procurement_scope=branch_scope_for_user(user, include_shared=False, tenant=tenant),
        )


EVERY_BLOCK = DashboardReader()


# Wrap integer kobo in standard money response shape.
def _m(kobo) -> dict:
    """Money envelope matching the rest of the finance API: ``{kobo, naira}``."""
    kobo = int(kobo or 0)  # Normalize missing values to zero.
    return {"kobo": kobo, "naira": format_naira(kobo)}  # Include raw kobo and formatted naira.


# Build a display label for a user or system actor.
def _user_label(user) -> str:
    """Human label for an actor - the custom User has no ``get_full_name``."""
    if user is None:  # System-generated rows have no user.
        return "system"
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()  # Build full name defensively.
    return name or getattr(user, "email", "") or "system"  # Prefer name, fallback to email/system.


# Compute signed percentage movement.
def _pct_change(curr: int, prev: int) -> float | None:
    """Signed % change vs the prior point, or ``None`` when there's no base."""
    if not prev:  # Avoid divide-by-zero and undefined base.
        return None
    return round((curr - prev) * 100 / abs(prev), 1)  # Return one-decimal percent delta.


# --------------------------------------------------------------------------- #
# Period window                                                               #
# --------------------------------------------------------------------------- #

# Resolve dashboard anchor period.
def _current_period(entity, period=None):
    """The period the dashboard is based on.

    When the caller pins a ``period`` we use it. Otherwise the *default* is the
    period that **contains today** (so the as-of is the present day), falling back
    to the latest open period, then the latest period.
    """
    qs = FiscalPeriod.objects.filter(entity=entity).select_related("fiscal_year")
    if period is not None:  # Caller explicitly pinned a period.
        return period
    today = datetime.date.today()
    containing = (  # Prefer the period containing today.
        qs.filter(start_date__lte=today, end_date__gte=today)
        .order_by("-fiscal_year__year", "-period_no")
        .first()
    )
    return (  # Fallback chain when today has no period.
        containing  # Current calendar period.
        or qs.filter(status=PeriodStatus.OPEN).order_by("-fiscal_year__year", "-period_no").first()
        or qs.order_by("-fiscal_year__year", "-period_no").first()
    )


# Format fiscal year span for dashboard header.
def _fiscal_year_label(current) -> str | None:
    """A span label like ``2025/2026`` (or ``2026`` if the year is single-calendar)."""
    fy = getattr(current, "fiscal_year", None)  # Current period may be absent.
    if fy is None:  # No fiscal year to label.
        return None
    s, e = fy.start_date.year, fy.end_date.year  # Fiscal year start/end calendar years.
    return f"{s}/{e}" if s != e else str(fy.year)  # Use span only when crossing calendar years.


# Build trailing fiscal-period window.
def _period_window(entity, current, n=SPARK_POINTS):
    """The up-to-``n`` fiscal periods ending at ``current`` (ascending)."""
    all_p = list(  # Load all entity periods in chronological order.
        FiscalPeriod.objects.filter(entity=entity)
        .select_related("fiscal_year")
        .order_by("fiscal_year__year", "period_no")
    )
    if current is not None:  # Trim to periods ending at the current anchor.
        idx = next((i for i, p in enumerate(all_p) if p.id == current.id), len(all_p) - 1)  # Find anchor index.
        all_p = all_p[: idx + 1]  # Keep periods up to anchor.
    return all_p[-n:]  # Return final n periods.


# Build cumulative closing-balance series.
def _closing_series(account_ids, window_periods, normal) -> list[int]:
    """Closing balance of an account set at each window period-end, signed to ``normal``.

    In this denormalised model each :class:`AccountBalance` row holds only that
    period's *movement* (opening is 0) and rows exist only for periods with activity.
    So the closing balance at period ``p`` is the **cumulative** signed movement over
    every period up to and including ``p`` - including movements that predate the
    sparkline window (e.g. an opening capital injection in period 1).
    """
    if not account_ids or not window_periods:  # No accounts or periods means zero series.
        return [0 for _ in window_periods]
    sign = 1 if normal == NormalBalance.DEBIT else -1  # Convert movements to natural-balance sign.
    rows = (  # Aggregate account movements by fiscal period.
        AccountBalance.objects.filter(account_id__in=account_ids)
        .values("period__fiscal_year__year", "period__period_no")
        .annotate(dr=Sum("debit_total"), cr=Sum("credit_total"))
    )
    moves: dict[tuple, int] = {}  # Movement by fiscal period key.
    for r in rows:  # Convert aggregate rows to signed movements.
        key = (r["period__fiscal_year__year"], r["period__period_no"])  # Comparable period key.
        moves[key] = moves.get(key, 0) + sign * ((r["dr"] or 0) - (r["cr"] or 0))
    out = []  # Closing balances at each window point.
    for p in window_periods:  # Compute cumulative balance through each period.
        pk = (p.fiscal_year.year, p.period_no)  # Current period key.
        out.append(int(sum(v for k, v in moves.items() if k <= pk)))  # Sum all prior/current movement.
    return out  # Return closing series.


# Build YTD net-income series.
def _net_income_series(entity, window_periods) -> list[int]:
    """Cumulative YTD net income at each window period-end.

    Every income/expense leg contributes ``credit − debit`` to net income (income is
    credit-natural so adds; expense is debit-natural so its ``c−d`` is negative and
    subtracts). Accumulated within each period's fiscal year up to its period number -
    so it's true year-to-date even when the window starts mid-year.
    """
    if not window_periods:  # No period window means no series.
        return []
    rows = (  # Aggregate income/expense movements by period.
        AccountBalance.objects.filter(
            account__entity=entity,  # Scope to entity.
            account__account_type__in=[AccountType.INCOME, AccountType.EXPENSE],  # Income and expenses only.
        )
        .values("period__fiscal_year__year", "period__period_no")
        .annotate(d=Sum("debit_total"), c=Sum("credit_total"))
    )
    net_by: dict[tuple, int] = {}  # Net income movement by period key.
    for r in rows:  # Convert rows to net P&L movement.
        key = (r["period__fiscal_year__year"], r["period__period_no"])  # Comparable period key.
        net_by[key] = net_by.get(key, 0) + ((r["c"] or 0) - (r["d"] or 0))
    out = []  # YTD series values.
    for p in window_periods:  # Build value for each period.
        yr, pno = p.fiscal_year.year, p.period_no  # Current fiscal year and period number.
        out.append(int(sum(v for (y, n), v in net_by.items() if y == yr and n <= pno)))  # Sum YTD within fiscal year.
    return out  # Return YTD net-income series.


# Convert a raw series to dashboard KPI shape.
def _kpi(series: list[int]) -> dict:
    curr = series[-1] if series else 0  # Current value is final point.
    prev = series[-2] if len(series) > 1 else 0  # Prior value drives delta.
    return {"value": _m(curr), "delta_pct": _pct_change(curr, prev), "spark": [int(v) for v in series]}  # KPI payload.


# --------------------------------------------------------------------------- #
# Cross-module account sets                                                    #
# --------------------------------------------------------------------------- #

# Resolve GL account ids that count as cash.
def _cash_account_ids(entity) -> set:
    """Cash accounts = the canonical ``1100 Cash & Bank`` GL account plus any GL
    account a :class:`BankAccount` maps to - the same definition the cash-flow
    statement uses, so the dashboard's cash position reconciles to it. (Many
    entities hold cash directly in ``1100`` with no operational BankAccount row.)
    """
    from .account_mappings import resolve_mapped_account
    from .constants import AccountMappingKey

    ids = {resolve_mapped_account(entity, AccountMappingKey.CASH_BANK).id}
    ids |= set(  # Include operational bank accounts mapped to GL accounts.
        BankAccount.objects.filter(entity=entity)
        .exclude(gl_account=None)
        .values_list("gl_account_id", flat=True)
    )
    return ids  # Return cash-equivalent account ids.


# Resolve vendor payable account ids best-effort.
def _payable_account_ids(entity) -> set:
    try:  # Procurement may be unavailable or misconfigured.
        from vs_procurement.models import Vendor

        return set(  # Vendor payable control account ids.
            Vendor.objects.filter(entity=entity)
            .exclude(payable_account=None)
            .values_list("payable_account_id", flat=True)
        )
    except Exception:  # pragma: no cover - procurement optional
        return set()  # Degrade to no payables instead of breaking dashboard.


# --------------------------------------------------------------------------- #
# Blocks                                                                       #
# --------------------------------------------------------------------------- #

# Build revenue/expense actual-vs-plan block.
def _revenue_vs_budget(entity, fiscal_year) -> dict:
    from .reports import budget_vs_actual, income_statement

    pnl = income_statement(entity)  # YTD, whole open year
    rev_actual, exp_actual = pnl.total_income, pnl.total_expense  # Actual P&L totals.

    budget = None  # Optional approved/latest budget.
    if fiscal_year is not None:  # Budget lookup requires a fiscal year.
        from .models import Budget

        budget = (  # Choose latest budget for the fiscal year.
            Budget.objects.filter(entity=entity, fiscal_year=fiscal_year)
            .order_by("-approved_at", "-id")
            .first()
        )
    rev_plan = exp_plan = 0  # Budget totals default to zero.
    if budget is not None:  # Compute plan totals when a budget exists.
        rep = budget_vs_actual(budget)  # Reuse budget-vs-actual report rows.
        for r in rep.rows:  # Sum budget by P&L account type.
            if r.account_type == AccountType.INCOME:  # Income budget row.
                rev_plan += r.budget  # Add revenue plan.
            elif r.account_type == AccountType.EXPENSE:  # Expense budget row.
                exp_plan += r.budget  # Add expense plan.

    # Shape one actual-vs-plan line.
    def line(actual, plan):
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
    }


# Build AR aging chart block.
def _ar_aging_block(entity, as_of, scope=UNNARROWED) -> dict:
    from .reports import ar_aging

    rep = ar_aging(entity, as_of=as_of, scope=scope)  # Compute aging report as of dashboard date.
    total = rep.total_net or 0  # Total outstanding AR.
    buckets = []  # Aging bucket payload rows.
    for key, amount in rep.bucket_totals.items():  # Convert bucket totals to display shape.
        amount = int(amount or 0)  # Normalize missing amount.
        pct = round(amount * 100 / total) if total else 0  # Bucket share of total.
        buckets.append({"key": key, "pct": pct, "amount": _m(amount)})  # Append bucket payload.
    return {"buckets": buckets, "total": _m(total)}  # Return aging block.


# Build trailing receivables-issued vs collections trend.
def _trend(entity, anchor, *, scope=UNNARROWED, issued_ok=True, collected_ok=True) -> dict:
    """Trailing-12-month receivable-issued vs collected ending at ``anchor``'s month.

    A series the reader may not read is ``None`` rather than a row of zeros, which
    would read as "nothing was invoiced".
    """
    first = anchor.replace(day=1)  # Anchor to first day of dashboard month.
    # step back 11 months for a 12-point window  # Inclusive current month.
    y, mo = first.year, first.month - (TREND_MONTHS - 1)  # Raw starting month.
    while mo <= 0:  # Roll back across calendar years.
        mo += 12  # Normalize month into 1-12.
        y -= 1  # Move to previous year.
    start = datetime.date(y, mo, 1)

    issued = {  # Posted invoice totals by month.
        r["m"]: int(r["s"] or 0)  # Month bucket -> total issued.
        for r in scope.filter(Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=start  # Entity, posted, after start.
        ))
        .annotate(m=TruncMonth("invoice_date"))
        .values("m")
        .annotate(s=Sum("total"))
    }
    collected = {  # Posted payment totals by month.
        r["m"]: int(r["s"] or 0)  # Month bucket -> total collected.
        for r in scope.filter(Payment.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start  # Entity, posted, after start.
        ))
        .annotate(m=TruncMonth("payment_date"))
        .values("m")
        .annotate(s=Sum("amount"))
    }
    labels, iss, col = [], [], []  # Chart labels and two series.
    cur = start  # Current month cursor.
    for _ in range(TREND_MONTHS):  # Build fixed-length trend arrays.
        key = datetime.date(cur.year, cur.month, 1)
        labels.append(cur.strftime("%b %y"))  # Human month label.
        iss.append(issued.get(key, 0))
        col.append(collected.get(key, 0))
        cur = datetime.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return {  # Return trend block.
        "labels": labels,
        "issued": iss if issued_ok else None,
        "collected": col if collected_ok else None,
    }


# Return top overdue customer invoices.
def _top_overdue(entity, as_of, scope=UNNARROWED) -> list[dict]:
    today = as_of or datetime.date.today()
    qs = (  # Query open overdue invoices.
        scope.filter(Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=today  # Entity, posted, overdue.
        ))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
        .filter(bal__gt=0)
        .select_related("customer")
        .order_by("-bal")[:TOP_OVERDUE]
    )
    return [  # Shape overdue rows.
        {
            "customer": i.customer.name,  # Customer name.
            "customer_code": i.customer.code,  # Customer code.
            "reference": i.document_number,  # Invoice number.
            "amount": _m(i.bal),  # Outstanding amount.
            "days_overdue": (today - i.due_date).days,  # Age past due.
        }
        for i in qs  # Iterate top overdue invoices.
    ]


# Return upcoming vendor bills best-effort.
def _vendor_due(entity, scope=UNNARROWED) -> list[dict]:
    try:  # Procurement app is optional for dashboard resilience.
        from vs_procurement.models import VendorInvoice

        today = datetime.date.today()
        end = today + datetime.timedelta(days=VENDOR_DUE_DAYS)
        qs = (  # Query upcoming open vendor bills.
            scope.filter(VendorInvoice.objects.filter(
                entity=entity,  # Scope to entity.
                status=DocumentStatus.POSTED,  # Posted vendor invoices only.
                due_date__gte=today,  # Not already overdue.
                due_date__lte=end,  # Due within configured window.
            ))
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .annotate(bal=F("total") - F("amount_paid"))
            .filter(bal__gt=0)
            .select_related("vendor")
            .order_by("due_date")[:TOP_OVERDUE]
        )
        return [  # Shape vendor due rows.
            {
                "vendor": v.vendor.name,  # Vendor name.
                "reference": v.document_number,  # Bill number.
                "due_date": v.due_date.isoformat(),  # ISO due date.
                "amount": _m(v.bal),  # Outstanding amount.
                "days_until": (v.due_date - today).days,  # Days until due.
            }
            for v in qs  # Iterate vendor bills.
        ]
    except Exception:  # pragma: no cover - procurement optional
        return []  # Degrade to empty vendor list.


# Count pending procurement approval overlays.
def _approvals(entity, reader=EVERY_BLOCK) -> dict:
    """Pending spend-approvals, counted from each procurement doc's ``approval_state``.

    Entity-scoped and read straight off the document overlay (no cross-app workflow
    join), so it's exact and can't leak another entity's counts. Each document type
    is counted only for a reader who may list it, and only over their branches.
    """
    items = []  # Approval count rows.
    try:  # Procurement app is optional for dashboard resilience.
        from vs_procurement import models as pm
        from vs_procurement.constants import ProcApprovalState

        pending = ProcApprovalState.PENDING  # Pending approval state.
        specs = [  # Procurement documents shown on dashboard.
            ("PurchaseRequisition", "Purchase requisitions", "procurement.requisition.view"),
            ("PurchaseOrder", "Purchase orders", "procurement.purchase_order.view"),
            ("VendorInvoice", "Vendor invoices", "procurement.vendor_invoice.view"),
        ]
        for model_name, label, key in specs:  # Count each procurement document type.
            model = getattr(pm, model_name, None)  # Resolve model defensively.
            if model is None or not reader.can(key):  # Skip unavailable or unreadable type.
                continue
            count = reader.procurement_scope.filter(
                model.objects.filter(entity=entity, approval_state=pending)).count()
            items.append({"label": label, "count": count})  # Add count row.
    except Exception:  # pragma: no cover - procurement optional
        pass  # Degrade to empty approvals list.
    return {"items": items, "total": sum(i["count"] for i in items)}  # Return counts and total.


# Summarize period close checklist status.
def _close_progress(entity, period) -> dict | None:
    if period is None:  # No period means no close checklist.
        return None
    from .close import close_checklist

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
    }


# Warn when the entity is about to run out of fiscal calendar.
def _fiscal_runway(entity) -> dict:
    """Dashboard shape of :func:`vs_finance.posting.fiscal_calendar_runway`.

    Read as of **today** even when the caller pins a historical period: "can this
    entity still post?" is a question about now, not about the period being reported
    on, and a dashboard pinned to last March must not report a runway that has since
    lapsed as healthy.
    """
    runway = fiscal_calendar_runway(entity)  # Same read the posting guard mirrors.
    end = runway["calendar_end"]  # Last day any period covers (None when none exist).
    return {  # Return fiscal-runway block.
        "status": runway["status"],  # HEALTHY / EXPIRING / EXPIRED.
        "calendar_end": end.isoformat() if end else None,  # ISO last postable day.
        "days_remaining": runway["days_remaining"],  # Negative once lapsed, None when no calendar.
        "threshold_days": runway["threshold_days"],  # Notice window the status used.
    }


# Return recent journal activity rows.
def _recent_journals(entity, limit=5, scope=UNNARROWED) -> list[dict]:
    from .models import JournalEntry

    qs = (  # Recent journals for entity.
        scope.filter(JournalEntry.objects.filter(entity=entity))
        .select_related("created_by")
        .order_by("-date", "-id")[:limit]
    )
    out = []  # Recent journal payload rows.
    for j in qs:  # Shape each journal row.
        dr, _cr = j.totals()  # Journal amount from debit side.
        out.append(  # Append dashboard row.
            {
                "document_number": j.document_number,  # Journal number.
                "date": j.date.isoformat(),  # ISO journal date.
                "source": getattr(j, "source", "") or "Manual",  # Journal source label.
                "narration": getattr(j, "narration", "") or "",  # Journal narration.
                "amount": _m(dr),  # Journal amount.
                "status": j.status,  # Journal status.
                "created_by": _user_label(getattr(j, "created_by", None)),  # Creator label.
            }
        )
    return out  # Return recent journals.


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
# Thin typed wrapper for dashboard payloads.
class FinanceDashboard:
    payload: dict = field(default_factory=dict)  # Dashboard response payload.


# Assemble complete finance dashboard payload.
def finance_dashboard(entity, *, period=None, reader=EVERY_BLOCK) -> dict:
    """Assemble the Finance-overview payload for ``entity`` as ``reader`` may see it.

    Each block is ``None`` when the reader may not see it; see the module docstring
    for which keys and which reach each block needs. The receivables card is read
    off the GL for a whole-school report reader, and off the reader's own open
    invoices otherwise, so a branch bursar sees their branch's outstanding balance
    rather than nothing.
    """
    current = _current_period(entity, period)  # Resolve anchor period.
    # Default as-of is the present day; pinning a period moves it to that period's end.  # Makes historical dashboards deterministic.
    if period is not None and current is not None:  # Caller pinned a specific period.
        as_of = current.end_date  # Use period end as dashboard date.
    else:  # Live dashboard uses today.
        as_of = datetime.date.today()
    periods = _period_window(entity, current)  # KPI sparkline period window.

    ledger = reader.whole_tenant and reader.can("finance.report.view")  # GL-derived blocks.
    invoices = reader.can("finance.invoice.view")
    payments = reader.can("finance.payment.view")
    scope = reader.scope

    kpis = {"cash_position": None, "receivables": None, "payables": None, "net_income_ytd": None}
    aging = None
    if invoices or ledger:
        aging = _ar_aging_block(entity, as_of, scope)
    if ledger:
        kpis["cash_position"] = _kpi(_closing_series(_cash_account_ids(entity), periods, NormalBalance.DEBIT))
        kpis["receivables"] = _kpi(_closing_series(
            set(  # Customer AR account ids.
                Customer.objects.filter(entity=entity)
                .exclude(receivable_account=None)
                .values_list("receivable_account_id", flat=True)
            ),
            periods,
            NormalBalance.DEBIT,  # AR is debit-natural.
        ))
        kpis["payables"] = _kpi(_closing_series(_payable_account_ids(entity), periods, NormalBalance.CREDIT))
        kpis["net_income_ytd"] = _kpi(_net_income_series(entity, periods))
    elif invoices:
        # The reader's own open invoices: no GL history to draw a sparkline from.
        kpis["receivables"] = {"value": aging["total"], "delta_pct": None, "spark": []}

    approvals = _approvals(entity, reader)
    return {  # Complete dashboard payload.
        "entity": entity.code,  # Entity code.
        "fiscal_year": _fiscal_year_label(current),  # Fiscal year label.
        "period": getattr(current, "name", None),  # Current period name.
        "as_of": as_of.isoformat(),  # Dashboard as-of date.
        "narrowed": not reader.whole_tenant,  # Figures cover only the reader's branches.
        "fiscal_runway": _fiscal_runway(entity),  # Fiscal-calendar expiry warning.
        "kpis": kpis,  # Executive KPI cards.
        "revenue_vs_budget": (
            _revenue_vs_budget(entity, getattr(current, "fiscal_year", None)) if ledger else None
        ),
        "ar_aging": aging,  # AR aging block.
        "trend": (
            _trend(entity, as_of, scope=scope, issued_ok=invoices, collected_ok=payments)
            if invoices or payments else None
        ),
        "top_overdue": _top_overdue(entity, as_of, scope) if invoices else None,
        "vendor_due": (
            _vendor_due(entity, reader.procurement_scope)
            if reader.can("procurement.vendor_invoice.view") else None
        ),
        "approvals": approvals if approvals["items"] else None,
        "close_progress": (
            _close_progress(entity, current)
            if reader.whole_tenant and reader.can("finance.period.view") else None
        ),
        "recent_journals": (
            _recent_journals(entity, scope=scope) if reader.can("finance.journal.view") else None
        ),
    }
