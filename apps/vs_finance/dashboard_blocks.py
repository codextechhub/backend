"""The finance overview's window-aware and operational cards.

:mod:`vs_finance.dashboard` assembles the payload; this module computes the cards
that answer "for which window?" and the cards that list what needs doing next.

The window
    A dashboard reads one window at a time: this month, this quarter, the fiscal
    year to date, or the billing period its owner bills by (a school's term; see
    :mod:`vs_finance.billing_periods`). A calendar window collects by date: what
    was invoiced and what was received between its first day and today. A
    billing period collects by the fees billed for it: the invoices that belong
    to it and what has been paid against them, whenever that money arrived. The
    two answer different questions, so each card says which basis it used.

Books
    A school's books (the tenant is a school) get school words on screen, and a
    term window when the school has a current term. Any other books get neutral
    words and calendar windows. The server says which, so both applications
    render the same books the same way.

Every card is computed only for a reader who holds the key behind it, and a
branch-bound reader's cards answer under their branches, as in
:mod:`vs_finance.dashboard`. Cards whose figures are the school's money as a
whole (bank balances, payroll, tax, the per-branch comparison) are for readers
who see the whole school.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Count, F, Min, Q, Sum
from django.db.models.functions import Abs

from vs_rbac.scoping import UNNARROWED

from .billing_periods import current_billing_period
from .constants import DocumentStatus, InvoicePaymentStatus
from .money import format_naira

UPCOMING_DAYS = 30
TAX_NOTICE_DAYS = 14
TOP_PAYERS = 5
UPCOMING_LIMIT = 6

#: Workflow document types an approver in finance acts on, and what to call them.
APPROVAL_TYPES = {
    "finance.write_off": ("write-off", "write-offs"),
    "finance.refund": ("refund", "refunds"),
    "finance.concession": ("concession", "concessions"),
    "finance.expense_claim": ("expense claim", "expense claims"),
    "finance.journal": ("journal", "journals"),
    "payments.payout_batch": ("payout", "payouts"),
}

METHOD_LABELS = {
    "BANK_TRANSFER": "Bank transfer",
    "ONLINE": "Online payment",
    "CARD": "Card (POS)",
    "CASH": "Cash",
    "CHEQUE": "Cheque",
    "OTHER": "Other",
}


def _m(kobo) -> dict:
    kobo = int(kobo or 0)
    return {"kobo": kobo, "naira": format_naira(kobo)}


def _ratio(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


# --------------------------------------------------------------------------- #
# Books and windows                                                           #
# --------------------------------------------------------------------------- #

def books_kind(entity) -> str:
    """``"school"`` for a school's books, ``"general"`` for any other."""
    from vs_tenants.models import Tenant

    tenant = getattr(entity, "tenant", None)
    return "school" if tenant is not None and tenant.kind == Tenant.Kind.SCHOOL else "general"


@dataclass(frozen=True)
class Window:
    """The span a window-aware card reads, and how it reads it.

    ``invoices`` is set for a billing period (collect by the fees billed for it)
    and ``None`` for a calendar window (collect by date).
    """

    key: str
    label: str
    name: str
    start: datetime.date
    end: datetime.date
    invoices: Q | None = None

    @property
    def basis(self) -> str:
        return "billed_for" if self.invoices is not None else "dates"

    def payload(self) -> dict:
        return {
            "key": self.key, "label": self.label, "name": self.name,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "basis": self.basis,
        }


def _fiscal_year_start(current, as_of: datetime.date) -> datetime.date:
    fy = getattr(current, "fiscal_year", None)
    return fy.start_date if fy is not None else as_of.replace(month=1, day=1)


def available_windows(entity, as_of, current) -> list[Window]:
    """The windows these books can be read by, the default first."""
    month = Window("month", "This month", as_of.strftime("%B %Y"), as_of.replace(day=1), as_of)
    fy_start = _fiscal_year_start(current, as_of)
    fy_label = getattr(getattr(current, "fiscal_year", None), "year", as_of.year)
    year = Window("year", "Year to date", f"FY {fy_label} to date", fy_start, as_of)

    period = current_billing_period(entity, as_of)
    if period is not None:
        term = Window(period.key, period.label, period.name, period.start, period.end, period.invoices)
        return [term, month, year]

    months_in = (as_of.year - fy_start.year) * 12 + as_of.month - fy_start.month
    q_index = months_in // 3
    q_month = fy_start.month + q_index * 3
    q_start = datetime.date(fy_start.year + (q_month - 1) // 12, (q_month - 1) % 12 + 1, 1)
    quarter = Window("quarter", "This quarter", f"Q{q_index + 1} FY {fy_label}", q_start, as_of)
    return [month, quarter, year]


def resolve_window(entity, as_of, current, key: str | None) -> tuple[Window, list[Window]]:
    """The window asked for (or the default), and every window on offer."""
    windows = available_windows(entity, as_of, current)
    chosen = next((w for w in windows if w.key == key), windows[0])
    return chosen, windows


# --------------------------------------------------------------------------- #
# Window-aware cards                                                          #
# --------------------------------------------------------------------------- #

def _window_invoices(entity, window: Window, scope):
    from .models import Invoice

    qs = scope.filter(Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED))
    if window.invoices is not None:
        return qs.filter(window.invoices)
    return qs.filter(invoice_date__gte=window.start, invoice_date__lte=window.end)


def _window_receipts(entity, window: Window, scope):
    """``(method, branch_ref, amount, receipts)`` rows of money received in the window.

    For a billing period the amount is what each receipt settled on the period's
    invoices; for a calendar window it is each receipt's amount.
    """
    from .models import Payment, PaymentAllocation

    if window.invoices is not None:
        from .models import Invoice

        period_invoices = Invoice.objects.filter(entity=entity).filter(window.invoices).values("id")
        allocations = scope.filter(PaymentAllocation.objects.filter(
            payment__entity=entity, payment__status=DocumentStatus.POSTED,
            invoice_id__in=period_invoices,
        ), "payment__")
        return allocations.values(method=F("payment__method"), branch_ref=F("payment__branch_id")) \
            .annotate(amount=Sum("amount"), receipts=Count("payment_id", distinct=True))
    qs = scope.filter(Payment.objects.filter(
        entity=entity, status=DocumentStatus.POSTED,
        payment_date__gte=window.start, payment_date__lte=window.end,
    ))
    return qs.values("method", branch_ref=F("branch_id")).annotate(amount=Sum("amount"), receipts=Count("id"))


def collections(entity, window: Window, *, scope=UNNARROWED, billed_ok=True, collected_ok=True) -> dict:
    """Billed and collected in the window, and the share collected."""
    billed = None
    invoice_count = None
    if billed_ok:
        agg = _window_invoices(entity, window, scope).aggregate(
            billed=Sum(F("total") - F("amount_credited")), n=Count("id"),
        )
        billed, invoice_count = int(agg["billed"] or 0), agg["n"]
    collected = None
    if collected_ok:
        if window.invoices is not None:
            collected = int(
                _window_invoices(entity, window, scope).aggregate(s=Sum("amount_paid"))["s"] or 0
            )
        else:
            collected = sum(int(r["amount"] or 0) for r in _window_receipts(entity, window, scope))
    return {
        "billed": _m(billed) if billed is not None else None,
        "invoice_count": invoice_count,
        "collected": _m(collected) if collected is not None else None,
        "rate_pct": _ratio(collected, billed) if billed is not None and collected is not None else None,
    }


def channels(entity, window: Window, *, scope=UNNARROWED) -> dict:
    """Money received in the window, by how it was paid."""
    by_method: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in _window_receipts(entity, window, scope):
        slot = by_method[row["method"] or "OTHER"]
        slot[0] += int(row["amount"] or 0)
        slot[1] += int(row["receipts"] or 0)
    total = sum(amount for amount, _ in by_method.values())
    items = sorted(
        (
            {"key": method, "label": METHOD_LABELS.get(method, method.title()),
             "amount": _m(amount), "receipts": count, "pct": _ratio(amount, total)}
            for method, (amount, count) in by_method.items() if amount
        ),
        key=lambda item: -item["amount"]["kobo"],
    )
    return {"total": _m(total), "receipts": sum(i["receipts"] for i in items), "items": items}


def branches(entity, window: Window, as_of: datetime.date) -> list[dict] | None:
    """Billed, collected and overdue per branch; ``None`` for a single-branch school."""
    from vs_tenants.models import Branch

    from .models import Invoice

    tenant = getattr(entity, "tenant", None)
    names = dict(Branch.objects.filter(tenant=tenant).values_list("id", "name")) if tenant else {}
    if len(names) < 2:
        return None
    billed = {
        r["branch_id"]: (int(r["billed"] or 0), int(r["paid"] or 0))
        for r in _window_invoices(entity, window, UNNARROWED)
        .values("branch_id").annotate(billed=Sum(F("total") - F("amount_credited")), paid=Sum("amount_paid"))
    }
    received = defaultdict(int)
    if window.invoices is None:
        for r in _window_receipts(entity, window, UNNARROWED):
            received[r["branch_ref"]] += int(r["amount"] or 0)
    overdue = {
        r["branch_id"]: int(r["bal"] or 0)
        for r in Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=as_of,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
        .values("branch_id")
        .annotate(bal=Sum(F("total") - F("amount_paid") - F("amount_credited")))
    }
    rows = []
    for branch_id in [*sorted(names, key=lambda b: names[b]), None]:
        b, paid = billed.get(branch_id, (0, 0))
        collected = paid if window.invoices is not None else received.get(branch_id, 0)
        owed = max(overdue.get(branch_id, 0), 0)
        if branch_id is None and not (b or collected or owed):
            continue
        rows.append({
            "branch_id": branch_id,
            "name": names.get(branch_id),
            "billed": _m(b), "collected": _m(collected),
            "rate_pct": _ratio(collected, b), "overdue": _m(owed),
        })
    return rows


# --------------------------------------------------------------------------- #
# Snapshot cards                                                              #
# --------------------------------------------------------------------------- #

def top_payers(entity, as_of: datetime.date, *, scope=UNNARROWED) -> list[dict]:
    """The payers owing the most past their due dates."""
    from .models import Invoice

    rows = (
        scope.filter(Invoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=as_of,
        ))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
        .filter(bal__gt=0)
        .values("customer_id", "customer__name", "customer__code", "customer__branch__name")
        .annotate(owed=Sum("bal"), invoices=Count("id"), oldest=Min("due_date"))
        .order_by("-owed")[:TOP_PAYERS]
    )
    return [
        {
            "customer_id": r["customer_id"],
            "name": r["customer__name"],
            "code": r["customer__code"],
            "branch": r["customer__branch__name"],
            "amount": _m(r["owed"]),
            "invoices": r["invoices"],
            "days_overdue": (as_of - r["oldest"]).days,
        }
        for r in rows
    ]


def bank_accounts(entity) -> list[dict]:
    """Each active bank account: its ledger balance and where its reconciliation stands."""
    from .constants import BankLineStatus
    from .models import AccountBalance, BankAccount, BankReconciliation, BankStatementLine

    accounts = list(BankAccount.objects.filter(entity=entity, is_active=True).order_by("-is_primary", "name"))
    if not accounts:
        return []
    gl_ids = [a.gl_account_id for a in accounts if a.gl_account_id]
    balances = {
        r["account_id"]: int(r["dr"] or 0) - int(r["cr"] or 0)
        for r in AccountBalance.objects.filter(account_id__in=gl_ids)
        .values("account_id")
        .annotate(dr=Sum(F("opening_debit") + F("debit_total")), cr=Sum(F("opening_credit") + F("credit_total")))
    }
    open_lines = {
        r["bank_account_id"]: (r["n"], int(r["amount"] or 0))
        for r in BankStatementLine.objects.filter(
            bank_account__in=accounts, status=BankLineStatus.UNMATCHED,
        ).values("bank_account_id").annotate(n=Count("id"), amount=Sum(Abs("amount")))
    }
    last_recon = {}
    for r in BankReconciliation.objects.filter(bank_account__in=accounts).order_by("bank_account_id", "-as_of_date"):
        last_recon.setdefault(r.bank_account_id, r.as_of_date)
    out = []
    for a in accounts:
        n, amount = open_lines.get(a.id, (0, 0))
        reconciled = last_recon.get(a.id)
        out.append({
            "id": a.id,
            "gl_account_id": a.gl_account_id,
            "name": a.name,
            "bank_name": a.bank_name,
            "balance": _m(balances.get(a.gl_account_id, 0)),
            "unmatched_lines": n,
            "unmatched_amount": _m(amount),
            "last_reconciled": reconciled.isoformat() if reconciled else None,
        })
    return out


def budget_lines(entity, revenue_vs_budget: dict | None, fiscal_year, as_of) -> dict | None:
    """The school's plan line by line: income, then the largest spending lines.

    Adds to the headline revenue-vs-budget block the three biggest expense lines
    and how much of the fiscal year has gone, so "93% used" can be read against
    "75% of the year gone".
    """
    if revenue_vs_budget is None or fiscal_year is None or not revenue_vs_budget.get("has_budget"):
        return None
    from .constants import AccountType
    from .models import Budget
    from .reports import budget_vs_actual

    budget = (
        Budget.objects.filter(entity=entity, fiscal_year=fiscal_year, branch__isnull=True)
        .order_by("-approved_at", "-id").first()
    )
    report = budget_vs_actual(budget)
    expense = sorted(
        (r for r in report.rows if r.account_type == AccountType.EXPENSE and r.budget),
        key=lambda r: -r.budget,
    )[:3]
    lines = [{
        "label": "Income", "kind": "income",
        "actual": revenue_vs_budget["revenue"]["actual"], "plan": revenue_vs_budget["revenue"]["plan"],
        "pct": revenue_vs_budget["revenue"]["pct_of_plan"],
    }]
    for r in expense:
        lines.append({
            "label": r.name, "kind": "expense",
            "actual": _m(r.actual), "plan": _m(r.budget), "pct": _ratio(r.actual, r.budget),
        })
    span = (fiscal_year.end_date - fiscal_year.start_date).days or 1
    gone = min(max((as_of - fiscal_year.start_date).days, 0), span)
    return {"budget_name": budget.name, "year_elapsed_pct": round(gone * 100 / span), "lines": lines}


# --------------------------------------------------------------------------- #
# What needs doing                                                            #
# --------------------------------------------------------------------------- #

def approvals_waiting_on(entity, user) -> dict | None:
    """Finance approvals whose current stage is waiting on ``user``, by kind."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from vs_workflow.models import WorkflowStageAction, WorkflowStageApprover

    snaps = list(
        WorkflowStageApprover.objects.filter(
            user=user,
            attempt=F("stage_instance__attempt"),
            stage_instance__status="ACTIVE",
            stage_instance__instance__status="IN_PROGRESS",
            stage_instance__instance__tenant=entity.tenant,
            stage_instance__instance__document_type__in=list(APPROVAL_TYPES),
        ).values("stage_instance_id", "stage_instance__attempt", "stage_instance__instance_id",
                 "stage_instance__instance__document_type")
    )
    if not snaps:
        return {"total": 0, "items": []}
    acted = set(
        WorkflowStageAction.objects.filter(
            actor=user, stage_instance_id__in={s["stage_instance_id"] for s in snaps},
            reversed_at__isnull=True, is_reversal_of__isnull=True,
        ).values_list("stage_instance_id", "attempt")
    )
    seen, counts = set(), defaultdict(int)
    for s in snaps:
        if (s["stage_instance_id"], s["stage_instance__attempt"]) in acted:
            continue
        if s["stage_instance__instance_id"] in seen:
            continue
        seen.add(s["stage_instance__instance_id"])
        counts[s["stage_instance__instance__document_type"]] += 1
    items = [
        {"type": t, "label": APPROVAL_TYPES[t][0 if n == 1 else 1], "count": n}
        for t, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return {"total": sum(counts.values()), "items": items}


def attention(entity, as_of, reader, user, *, banks: list[dict] | None) -> list[dict]:
    """Things someone should act on, most urgent first; each only for its key holders."""
    from .models import Payment, PaymentPlanInstallment, PettyCashFund, TaxFiling

    items = []
    scope = reader.scope

    waiting = approvals_waiting_on(entity, user)
    if waiting and waiting["total"]:
        items.append({
            "key": "approvals", "tone": "urgent",
            "title": f"{waiting['total']} approval{'s' if waiting['total'] != 1 else ''} waiting on you",
            "detail": " · ".join(f"{i['count']} {i['label']}" for i in waiting["items"]),
            "count": waiting["total"], "amount": None,
        })

    if banks:
        n = sum(b["unmatched_lines"] for b in banks)
        if n:
            worst = max(banks, key=lambda b: b["unmatched_lines"])
            items.append({
                "key": "bank_lines", "tone": "warning",
                "title": f"{n} bank line{'s' if n != 1 else ''} not matched",
                "detail": worst["name"] if len(banks) == 1 or n == worst["unmatched_lines"]
                else f"{worst['name']} and others",
                "count": n, "amount": _m(sum(b["unmatched_amount"]["kobo"] for b in banks)),
            })

    if reader.whole_tenant and reader.can("finance.tax.view"):
        due = (
            TaxFiling.objects.filter(
                entity=entity, due_date__isnull=False,
                due_date__lte=as_of + datetime.timedelta(days=TAX_NOTICE_DAYS),
            )
            .exclude(filing_status__in=["PAID", "CANCELLED"])
            .filter(amount_due__gt=F("amount_paid"))
            .select_related("obligation").order_by("due_date")
        )
        filings = list(due[:3])
        if filings:
            first = filings[0]
            days = (first.due_date - as_of).days
            when = f"overdue by {-days} day{'s' if days != -1 else ''}" if days < 0 else (
                "due today" if days == 0 else f"due in {days} day{'s' if days != 1 else ''}")
            items.append({
                "key": "tax", "tone": "urgent" if days < 0 else "warning",
                "title": f"{first.obligation.name} {when}",
                "detail": f"{first.period_start.strftime('%B')} period"
                + (f" · {first.obligation.authority_name}" if first.obligation.authority_name else ""),
                "count": len(filings), "amount": _m(first.amount_due - first.amount_paid),
            })

    if reader.can("finance.paymentplan.view"):
        behind = scope.filter(
            PaymentPlanInstallment.objects.filter(
                plan__entity=entity, plan__plan_status="ACTIVE", due_date__lt=as_of,
            ).exclude(status="PAID"),
            "plan__",
        )
        agg = behind.aggregate(plans=Count("plan_id", distinct=True), owed=Sum(F("amount") - F("amount_settled")))
        if agg["plans"]:
            items.append({
                "key": "plans_behind", "tone": "warning",
                "title": f"{agg['plans']} payment plan{'s' if agg['plans'] != 1 else ''} behind",
                "detail": "Missed at least one instalment",
                "count": agg["plans"], "amount": _m(agg["owed"]),
            })

    if reader.can("finance.payment.view"):
        agg = scope.filter(Payment.objects.filter(
            entity=entity, status=DocumentStatus.POSTED,
        )).filter(amount__gt=F("allocated_amount")).aggregate(
            n=Count("id"), amount=Sum(F("amount") - F("allocated_amount")),
        )
        if agg["n"]:
            items.append({
                "key": "unallocated", "tone": "info",
                "title": f"{agg['n']} receipt{'s' if agg['n'] != 1 else ''} not applied to an invoice",
                "detail": "Money received with no invoice settled yet",
                "count": agg["n"], "amount": _m(agg["amount"]),
            })

    if reader.can("finance.pettycash.view"):
        from .banking_settings import resolve_finance_banking_settings

        threshold = resolve_finance_banking_settings(entity).petty_cash_low_balance_threshold_bps
        low = [
            f for f in scope.filter(PettyCashFund.objects.filter(entity=entity, is_active=True, float_amount__gt=0))
            if f.current_balance * 10000 < f.float_amount * threshold
        ]
        if low:
            items.append({
                "key": "petty_cash", "tone": "info",
                "title": f"{len(low)} petty cash float{'s' if len(low) != 1 else ''} running low",
                "detail": " · ".join(f.name for f in low[:2]) + (" and others" if len(low) > 2 else ""),
                "count": len(low), "amount": _m(sum(f.current_balance for f in low)),
            })

    order = {"urgent": 0, "warning": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["tone"]])


def upcoming(entity, as_of, reader) -> list[dict]:
    """Money due in or out over the next 30 days, soonest first."""
    from .models import PaymentPlanInstallment, PayrollRun, TaxFiling

    end = as_of + datetime.timedelta(days=UPCOMING_DAYS)
    out = []
    if reader.whole_tenant and reader.can("finance.payrollrun.view"):
        for run in PayrollRun.objects.filter(
            entity=entity, pay_date__gte=as_of, pay_date__lte=end,
            run_status__in=["DRAFT", "POSTED"],
        ).annotate(heads=Count("lines")):
            out.append({
                "date": run.pay_date.isoformat(), "kind": "payroll", "direction": "out",
                "title": f"{run.period_label or 'Payroll'} payroll",
                "detail": f"{run.heads} staff · {'draft' if run.run_status == 'DRAFT' else 'accrued, not paid'}",
                "amount": _m(run.net_total or run.gross_total),
            })
    if reader.can("finance.paymentplan.view"):
        rows = reader.scope.filter(
            PaymentPlanInstallment.objects.filter(
                plan__entity=entity, plan__plan_status="ACTIVE",
                due_date__gte=as_of, due_date__lte=end,
            ).exclude(status="PAID"),
            "plan__",
        ).values("due_date").annotate(plans=Count("plan_id", distinct=True),
                                      amount=Sum(F("amount") - F("amount_settled")))
        for r in rows:
            out.append({
                "date": r["due_date"].isoformat(), "kind": "instalments", "direction": "in",
                "title": "Payment plan instalments",
                "detail": f"{r['plans']} plan{'s' if r['plans'] != 1 else ''} due",
                "amount": _m(r["amount"]),
            })
    if reader.can("procurement.vendor_invoice.view"):
        try:
            from vs_procurement.models import VendorInvoice

            rows = reader.procurement_scope.filter(
                VendorInvoice.objects.filter(
                    entity=entity, status=DocumentStatus.POSTED,
                    due_date__gte=as_of, due_date__lte=end,
                ).exclude(payment_status=InvoicePaymentStatus.PAID)
            ).values("due_date").annotate(n=Count("id"), amount=Sum(F("total") - F("amount_paid")))
            for r in rows:
                out.append({
                    "date": r["due_date"].isoformat(), "kind": "vendor_bills", "direction": "out",
                    "title": "Vendor bills due",
                    "detail": f"{r['n']} bill{'s' if r['n'] != 1 else ''}",
                    "amount": _m(r["amount"]),
                })
        except Exception:  # pragma: no cover - procurement optional
            pass
    if reader.whole_tenant and reader.can("finance.tax.view"):
        for f in TaxFiling.objects.filter(
            entity=entity, due_date__gte=as_of, due_date__lte=end,
        ).exclude(filing_status__in=["PAID", "CANCELLED"]).filter(
            amount_due__gt=F("amount_paid"),
        ).select_related("obligation"):
            out.append({
                "date": f.due_date.isoformat(), "kind": "tax", "direction": "out",
                "title": f"{f.obligation.name} remittance",
                "detail": f"{f.period_start.strftime('%B')} period"
                + (f" · {f.obligation.authority_name}" if f.obligation.authority_name else ""),
                "amount": _m(f.amount_due - f.amount_paid),
            })
    out.sort(key=lambda i: i["date"])
    return out[:UPCOMING_LIMIT]


def payables_due(entity, as_of, reader) -> dict | None:
    """How many vendor bills fall due in the next 30 days, and how many are late."""
    try:
        from vs_procurement.models import VendorInvoice
    except Exception:  # pragma: no cover - procurement optional
        return None
    open_bills = reader.procurement_scope.filter(
        VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
        .exclude(payment_status=InvoicePaymentStatus.PAID)
    )
    end = as_of + datetime.timedelta(days=UPCOMING_DAYS)
    due = open_bills.filter(due_date__gte=as_of, due_date__lte=end).aggregate(
        n=Count("id"), amount=Sum(F("total") - F("amount_paid")))
    late = open_bills.filter(due_date__lt=as_of).aggregate(
        n=Count("id"), amount=Sum(F("total") - F("amount_paid")))
    return {
        "due_count": due["n"], "due_amount": _m(due["amount"]),
        "overdue_count": late["n"], "overdue_amount": _m(late["amount"]),
    }


def receivables_summary(entity, as_of, *, scope=UNNARROWED) -> dict:
    """Who owes, who is late, and who falls due within a week, as counts of payers.

    Gives the aging and overdue-payer cards their closing line: "9 payers owe,
    oldest 22 days overdue" and "falling due in 7 days: 14 payers". Counts are of
    payers, not invoices, because a family with three unpaid invoices is one
    conversation for the bursar.
    """
    from .models import Invoice

    open_invoices = (
        scope.filter(Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
        .filter(bal__gt=0)
    )
    owing = open_invoices.aggregate(n=Count("customer_id", distinct=True))
    overdue = open_invoices.filter(due_date__lt=as_of).aggregate(
        n=Count("customer_id", distinct=True), amount=Sum("bal"), oldest=Min("due_date"))
    soon = open_invoices.filter(
        due_date__gte=as_of, due_date__lte=as_of + datetime.timedelta(days=7),
    ).aggregate(n=Count("customer_id", distinct=True), amount=Sum("bal"))
    return {
        "owing_payers": owing["n"],
        "overdue_payers": overdue["n"],
        "overdue_amount": _m(overdue["amount"]),
        "oldest_days_overdue": (as_of - overdue["oldest"]).days if overdue["oldest"] else None,
        "due_soon_payers": soon["n"],
        "due_soon_amount": _m(soon["amount"]),
    }
