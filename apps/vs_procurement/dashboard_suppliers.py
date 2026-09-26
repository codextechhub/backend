"""The Spend & suppliers view of the Procurement dashboard.

A second tab beside the overview, for whoever chooses and manages vendors. It
reads the same windows (this month, a school's term, the year to date; always by
date) and answers: how much has been spent against the plan, how concentrated
spending is, how well vendors deliver, how much buying happens without an
order, which RFQs are open, what competition saved, where spending lands by
branch, how long buying takes, and the state of the vendor base.

Against the plan
    Spend is set against the school's approved budget for the fiscal year, on
    the expense accounts purchasing posts to (the accounts on vendor bills,
    order lines, vendors, categories and catalogue items), so salary lines in
    the same budget never count. Both sides are net of tax, year to date. With
    no approved budget, or for a reader who sees only some branches (a school
    plan measures the whole school), the tile compares with the same point of
    the window before instead.

How long buying takes
    Median days per step, over the steps that ended in the window: requisition
    approval (asked to approved), sourcing and ordering (approved to ordered),
    delivery (ordered to first receipt) and billing to payment (bill date to
    payment date). A step with nothing ending in the window has no median.

Every block needs the key of the screen it summarises and answers under the
reader's branches; vendors, assessments and quotations are entity master data
and stay entity-wide. The branch comparison is for readers who see the whole
school.
"""
from __future__ import annotations

import datetime
import statistics
from collections import defaultdict

from django.db.models import Count, Max, Min, Q, Sum

from vs_finance.constants import DocumentStatus

from .dashboard import _money, _spend_kobo
from .models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseRequisition,
    RequestForQuotation,
    Vendor,
    VendorInvoice,
    VendorQuotation,
)

SCORECARD_ROWS = 8
OPEN_RFQS = 5
SAVINGS_ROWS = 4
CONCENTRATION_SHARE = 0.8

_LIVE_PO = Q(status__in=(DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL,
                         DocumentStatus.CANCELLED, DocumentStatus.REVERSED))


def _ratio(part, whole) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def _bq(branch_scope, prefix: str = "") -> Q:
    """The reader's branch narrowing along ``prefix`` to the branch column."""
    return branch_scope.q(prefix) if branch_scope is not None else Q()


# --------------------------------------------------------------------------- #
# Spend against the plan                                                      #
# --------------------------------------------------------------------------- #

def _purchasing_accounts(entity) -> set:
    """Every expense account purchasing can post to on these books."""
    from .models import CatalogItem, PurchaseOrderLine, VendorCategory, VendorInvoiceLine

    ids = set(VendorInvoiceLine.objects.filter(vendor_invoice__entity=entity)
              .values_list("expense_account_id", flat=True))
    ids |= set(PurchaseOrderLine.objects.filter(purchase_order__entity=entity)
               .values_list("expense_account_id", flat=True))
    for model in (Vendor, VendorCategory, CatalogItem):
        ids |= set(model.objects.filter(entity=entity).values_list("default_expense_account_id", flat=True))
    return ids - {None}


def against_plan(entity, fiscal_year, as_of, branch_filter) -> dict | None:
    """Net purchasing spend this fiscal year against the approved plan for those accounts."""
    from vs_finance.models import Budget, BudgetLine

    if fiscal_year is None:
        return None
    budget = (
        Budget.objects.filter(entity=entity, fiscal_year=fiscal_year, branch__isnull=True, status="APPROVED")
        .order_by("-approved_at", "-id").first()
    )
    if budget is None:
        return None
    accounts = _purchasing_accounts(entity)
    planned = BudgetLine.objects.filter(budget=budget, account_id__in=accounts).aggregate(t=Sum("amount"))["t"] or 0
    if not planned:
        return None
    spent = VendorInvoice.objects.filter(
        entity=entity, status=DocumentStatus.POSTED,
        invoice_date__gte=fiscal_year.start_date, invoice_date__lte=as_of,
    ).filter(branch_filter).aggregate(t=Sum("subtotal"))["t"] or 0
    span = (fiscal_year.end_date - fiscal_year.start_date).days or 1
    return {
        "budget_name": budget.name, "planned": _money(planned), "spent_ytd": _money(spent),
        "pct": _ratio(spent, planned),
        "year_elapsed_pct": round(min(max((as_of - fiscal_year.start_date).days, 0), span) * 100 / span),
    }


# --------------------------------------------------------------------------- #
# Vendors                                                                     #
# --------------------------------------------------------------------------- #

def concentration(report) -> dict:
    """How few vendors take four-fifths of the window's spend."""
    rows = [r.gross for r in report.by_vendor if r.gross > 0]
    total, running, needed = sum(rows), 0, 0
    for gross in sorted(rows, reverse=True):
        if total and running >= total * CONCENTRATION_SHARE:
            break
        running += gross
        needed += 1
    return {"vendors_with_spend": len(rows), "vendors_for_80pct": needed}


def _acceptance(entity, start, end, branch_scope) -> dict:
    """Accepted and rejected quantities on posted receipts in the window, by vendor."""
    rows = (
        GoodsReceivedNoteLine.objects.filter(
            grn__entity=entity, grn__status=DocumentStatus.POSTED,
            grn__received_date__gte=start, grn__received_date__lte=end,
        ).filter(_bq(branch_scope, "grn__"))
        .values("grn__vendor_id")
        .annotate(ok=Sum("accepted_qty"), bad=Sum("rejected_qty"),
                  rejected_lines=Count("id", filter=Q(rejected_qty__gt=0)))
    )
    return {r["grn__vendor_id"]: r for r in rows}


def deliveries(entity, start, end, before, branch_scope) -> dict:
    """On-time share of rated receipts and the accepted share of received quantity."""
    from .reports import vendor_performance

    def on_time(s, e):
        rows = vendor_performance(entity, start_date=s, end_date=e, branch_scope=branch_scope).rows
        on, late = sum(r.on_time_receipts for r in rows), sum(r.late_receipts for r in rows)
        return _ratio(on, on + late)

    accepted = _acceptance(entity, start, end, branch_scope)
    ok = sum(float(r["ok"] or 0) for r in accepted.values())
    bad = sum(float(r["bad"] or 0) for r in accepted.values())
    now = on_time(start, end)
    then = on_time(*before) if before else None
    return {
        "on_time_pct": now,
        "on_time_change_pts": round(now - then, 1) if now is not None and then is not None else None,
        "accepted_pct": _ratio(ok, ok + bad),
        "rejected_lines": sum(r["rejected_lines"] for r in accepted.values()),
    }


def scorecard(entity, start, end, as_of, branch_scope) -> list:
    """The window's largest vendors: spend, delivery, acceptance, open orders, latest grade."""
    from .reports import vendor_performance
    from .purchasing import po_receipt_stage

    rows = sorted(
        (r for r in vendor_performance(entity, start_date=start, end_date=end, branch_scope=branch_scope).rows
         if r.total_billed or r.total_ordered),
        key=lambda r: (-r.total_billed, -r.total_ordered),
    )[:SCORECARD_ROWS]
    ids = [r.vendor_id for r in rows]
    accepted = _acceptance(entity, start, end, branch_scope)
    open_orders = defaultdict(int)
    for po in (
        PurchaseOrder.objects.filter(entity=entity, vendor_id__in=ids).filter(_bq(branch_scope))
        .exclude(_LIVE_PO).exclude(approval_state="PENDING")
        .annotate(ordered=Sum("lines__quantity"), received=Sum("lines__received_qty"))
        .values("vendor_id", "ordered", "received")
    ):
        if po_receipt_stage(po["ordered"], po["received"]) != "RECEIVED":
            open_orders[po["vendor_id"]] += 1
    out = []
    for r in rows:
        a = accepted.get(r.vendor_id)
        ok, bad = (float(a["ok"] or 0), float(a["bad"] or 0)) if a else (0, 0)
        grade = r.latest_assessment
        out.append({
            "vendor_id": r.vendor_id, "name": r.name, "category": r.category or None,
            "spend": _money(r.total_billed),
            "on_time_pct": round(r.on_time_rate * 100, 1) if r.on_time_rate is not None else None,
            "accepted_pct": _ratio(ok, ok + bad),
            "open_orders": open_orders[r.vendor_id],
            "grade": grade.grade if grade else None,
            "score": grade.overall_score if grade else None,
        })
    return out


def vendor_base(entity, start, end, fiscal_start) -> dict:
    """Active, on hold, awaiting KYC, ordered from once this year, added in the window."""
    active = Vendor.objects.filter(entity=entity, is_active=True)
    once = (
        PurchaseOrder.objects.filter(entity=entity, order_date__gte=fiscal_start).exclude(_LIVE_PO)
        .values("vendor_id").annotate(n=Count("id")).filter(n=1).count()
    )
    return {
        "active": active.count(),
        "on_hold": active.filter(on_hold=True).count(),
        "awaiting_kyc": active.exclude(kyc_status="VERIFIED").count(),
        "ordered_once": once,
        "added": active.filter(created_at__date__gte=start, created_at__date__lte=end).count(),
    }


# --------------------------------------------------------------------------- #
# Sourcing                                                                    #
# --------------------------------------------------------------------------- #

def open_rfqs(entity, as_of, branch_scope) -> dict:
    """Issued RFQs, soonest closing first, with how many invited vendors have quoted."""
    from .constants import RfqStatus

    rfqs = list(
        RequestForQuotation.objects.filter(entity=entity, rfq_status=RfqStatus.ISSUED).filter(_bq(branch_scope))
        .annotate(
            invited=Count("invitations", distinct=True),
            quoted=Count("quotations", filter=~Q(quotations__quotation_status="DRAFT"), distinct=True),
        ).order_by("response_due_date", "id")
    )
    return {
        "count": len(rfqs),
        "items": [
            {
                "id": r.id, "title": r.title, "number": r.document_number or str(r.pk),
                "invited": r.invited, "quoted": r.quoted, "budget": _money(r.budget_estimate),
                "closes": r.response_due_date.isoformat() if r.response_due_date else None,
                "ready": bool(r.invited and r.quoted >= r.invited)
                or bool(r.response_due_date and r.response_due_date < as_of),
            }
            for r in rfqs[:OPEN_RFQS]
        ],
    }


def savings(entity, fiscal_start, as_of, branch_scope) -> dict:
    """Awarded price against the highest quote, for RFQs awarded this fiscal year."""
    awarded = list(
        VendorQuotation.objects.filter(
            entity=entity, quotation_status="AWARDED",
            awarded_po__order_date__gte=fiscal_start, awarded_po__order_date__lte=as_of,
        ).filter(_bq(branch_scope, "rfq__")).select_related("vendor__category")
    )
    highest = dict(
        VendorQuotation.objects.filter(rfq_id__in=[q.rfq_id for q in awarded])
        .exclude(quotation_status="DRAFT").values("rfq_id").annotate(top=Max("total"))
        .values_list("rfq_id", "top")
    )
    by_category, saved_total, top_total = defaultdict(int), 0, 0
    for q in awarded:
        top = highest.get(q.rfq_id) or q.total
        saved = max(top - q.total, 0)
        saved_total += saved
        top_total += top
        by_category[q.vendor.category.name if q.vendor.category_id else "Uncategorised"] += saved
    items = sorted(by_category.items(), key=lambda kv: -kv[1])[:SAVINGS_ROWS]
    return {
        "saved": _money(saved_total), "pct": _ratio(saved_total, top_total), "rfqs": len(awarded),
        "items": [{"name": n, "saved": _money(v)} for n, v in items if v > 0],
    }


# --------------------------------------------------------------------------- #
# Where it lands, and how long it takes                                       #
# --------------------------------------------------------------------------- #

def by_branch(entity, start, end) -> list:
    """Posted bills in the window by branch, school-wide bills as their own row."""
    rows = (
        VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED,
                                     invoice_date__gte=start, invoice_date__lte=end)
        .values("branch_id", "branch__name").annotate(billed=Sum("total")).order_by("-billed")
    )
    return [{"branch": r["branch__name"], "amount": _money(r["billed"])} for r in rows if r["billed"]]


def _median(values) -> float | None:
    return round(statistics.median(values), 1) if values else None


def cycle_times(entity, start, end, branch_scope) -> dict:
    """Median days for each buying step that ended in the window."""
    from vs_finance.constants import FinanceAuditAction
    from vs_finance.models import FinanceAuditLog

    from .models import VendorPaymentAllocation

    approved_at = {
        int(r["target_id"]): r["first"].date()
        for r in FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.REQUISITION_APPROVED,
        ).exclude(target_id="").values("target_id").annotate(first=Min("created_at"))
        if str(r["target_id"]).isdigit()
    }
    reqs = dict(PurchaseRequisition.objects.filter(entity=entity, pk__in=approved_at).filter(_bq(branch_scope))
                .values_list("pk", "request_date"))
    approval = [(approved_at[pk] - asked).days for pk, asked in reqs.items() if start <= approved_at[pk] <= end]

    ordering = []
    for po in PurchaseOrder.objects.filter(
        entity=entity, requisition__isnull=False, order_date__gte=start, order_date__lte=end,
    ).filter(_bq(branch_scope)).exclude(_LIVE_PO).values("order_date", "requisition_id", "requisition__request_date"):
        begun = approved_at.get(po["requisition_id"], po["requisition__request_date"])
        ordering.append(max((po["order_date"] - begun).days, 0))

    delivery = [
        max((r["first"] - r["purchase_order__order_date"]).days, 0)
        for r in GoodsReceivedNote.objects.filter(entity=entity, status=DocumentStatus.POSTED).filter(_bq(branch_scope))
        .values("purchase_order_id", "purchase_order__order_date").annotate(first=Min("received_date"))
        if r["purchase_order_id"] and start <= r["first"] <= end
    ]
    paying = [
        max((r["payment__payment_date"] - r["vendor_invoice__invoice_date"]).days, 0)
        for r in VendorPaymentAllocation.objects.filter(
            payment__entity=entity, payment__status=DocumentStatus.POSTED,
            payment__payment_date__gte=start, payment__payment_date__lte=end,
        ).filter(_bq(branch_scope, "payment__")).values("payment__payment_date", "vendor_invoice__invoice_date")
    ]
    steps = [
        {"key": "approval", "median_days": _median(approval), "samples": len(approval)},
        {"key": "ordering", "median_days": _median(ordering), "samples": len(ordering)},
        {"key": "delivery", "median_days": _median(delivery), "samples": len(delivery)},
        {"key": "payment", "median_days": _median(paying), "samples": len(paying)},
    ]
    known = [s["median_days"] for s in steps if s["median_days"] is not None]
    slowest = max((s for s in steps if s["median_days"] is not None), key=lambda s: s["median_days"], default=None)
    return {
        "steps": steps,
        "total_days": round(sum(known), 1) if len(known) == len(steps) else None,
        "slowest": slowest["key"] if slowest else None,
    }


# --------------------------------------------------------------------------- #
# The view                                                                    #
# --------------------------------------------------------------------------- #

def suppliers_view(entity, *, user=None, as_of=None, branch_scope=None, reader=None, window=None) -> dict:
    """The Spend & suppliers payload for ``entity`` as ``reader`` may see it."""
    from django.utils import timezone

    from vs_finance.dashboard import EVERY_BLOCK, _current_period
    from vs_finance.dashboard_blocks import books_kind, resolve_window
    from vs_finance.dashboard_spend import same_point_before, window_days

    from .reports import spend_analysis
    from .settings import resolve_procurement_settings

    reader = reader or EVERY_BLOCK
    narrowed = branch_scope is not None and branch_scope.is_narrowed
    as_of = as_of or timezone.localdate()
    current = _current_period(entity, None)
    chosen, windows = resolve_window(entity, as_of, current, window)
    start, end = window_days(chosen, as_of)
    before = same_point_before(entity, chosen, as_of)
    fiscal = getattr(current, "fiscal_year", None)
    fiscal_start = fiscal.start_date if fiscal is not None else as_of.replace(month=1, day=1)
    branch_filter = branch_scope.q() if branch_scope is not None else Q()
    analytics = reader.can("procurement.analytics.view")

    spend = None
    report = None
    if analytics:
        report = spend_analysis(entity, start_date=start, end_date=end, branch_scope=branch_scope)
        previous = _spend_kobo(entity, *before, branch_filter) if before else None
        spend = {
            "value": _money(report.total_gross),
            "prior_value": _money(previous) if previous is not None else None,
            "delta_pct": _ratio(report.total_gross - previous, previous) if previous else None,
            "plan": None if narrowed else against_plan(entity, fiscal, as_of, branch_filter),
            **concentration(report),
        }

    vendors_paid = None
    if reader.can("procurement.vendor_payment.view"):
        from .models import VendorPayment

        vendors_paid = VendorPayment.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start, payment_date__lte=end,
        ).filter(branch_filter).values("vendor_id").distinct().count()

    non_po = None
    if reader.can("procurement.vendor_invoice.view"):
        bills = VendorInvoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, invoice_date__gte=start, invoice_date__lte=end,
        ).filter(branch_filter)
        agg = bills.aggregate(billed=Sum("total"), direct=Sum("total", filter=Q(purchase_order__isnull=True)),
                              n=Count("id", filter=Q(purchase_order__isnull=True)))
        non_po = {
            "amount": _money(agg["direct"]), "count": agg["n"], "pct": _ratio(agg["direct"] or 0, agg["billed"] or 0),
            "limit_pct": resolve_procurement_settings(entity).non_po_spend_limit_pct,
        }

    return {
        "entity": entity.code,
        "currency": entity.base_currency_id,
        "books": books_kind(entity),
        "reader_first_name": (getattr(user, "first_name", "") or "").strip() or None,
        "as_of": as_of.isoformat(),
        "narrowed": narrowed,
        "window": {**chosen.payload(), "basis": "dates"},
        "windows": [{"key": w.key, "label": w.label, "name": w.name} for w in windows],
        "spend": spend,
        "vendors_paid": vendors_paid,
        "deliveries": (
            deliveries(entity, start, end, before, branch_scope)
            if reader.can("procurement.goods_receipt.view") else None
        ),
        "non_po": non_po,
        "scorecard": scorecard(entity, start, end, as_of, branch_scope) if analytics else None,
        "open_rfqs": open_rfqs(entity, as_of, branch_scope) if reader.can("procurement.rfq.view") else None,
        "savings": (
            savings(entity, fiscal_start, as_of, branch_scope) if reader.can("procurement.quotation.view") else None
        ),
        "by_branch": by_branch(entity, start, end) if analytics and not narrowed else None,
        "cycle_times": cycle_times(entity, start, end, branch_scope) if analytics else None,
        "vendor_base": vendor_base(entity, start, end, fiscal_start) if reader.can("procurement.vendor.view") else None,
    }
