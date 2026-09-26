"""Entity-scoped aggregate for the Procurement overview.

The dashboard is deliberately assembled on the server: list endpoints are paginated,
workflow approvals are actor-specific, and all blocks need to share one ``as_of``
snapshot.  Only display-safe fields leave this module; raw audit/workflow metadata is
never exposed.

It reads the finance dashboard's windows (see :mod:`vs_finance.dashboard_blocks`),
always by date, so a school's "This term" means the term's days here. The
pipeline runs from requisition to payment, one stage per list, and says where
every open document sits now; the exceptions are the controls that failed or are
waiting on someone.
"""
from __future__ import annotations

import calendar
import datetime

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from vs_finance.constants import (
    DocumentStatus,
    FinanceAuditAction,
    FinanceAuditStatus,
    InvoicePaymentStatus,
)
from vs_finance.models import FinanceAuditLog
from vs_finance.money import format_naira

from .constants import (
    PROCUREMENT_APPROVAL_TYPES,
    ProcApprovalState,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)
from .models import (
    PurchaseOrder,
    PurchaseRequisition,
    Vendor,
    VendorInvoice,
    VendorPayment,
)


SLOW_APPROVAL_DAYS = 5
APPROVALS_SHOWN = 5
TOP_VENDORS = 5
UNBILLED_RECEIPT_DAYS = 30
CONTRACT_HORIZON_DAYS = 90
CONTRACTS_SHOWN = 4

PROCUREMENT_AUDIT_ACTIONS = (
    # Keep this allow-list explicit so unrelated finance events never enter the feed.
    FinanceAuditAction.REQUISITION_APPROVED,
    FinanceAuditAction.RFQ_ISSUED,
    FinanceAuditAction.RFQ_CANCELLED,
    FinanceAuditAction.QUOTATION_SUBMITTED,
    FinanceAuditAction.QUOTATION_AWARDED,
    FinanceAuditAction.VENDOR_CONTRACT_ACTIVATED,
    FinanceAuditAction.VENDOR_CONTRACT_RENEWED,
    FinanceAuditAction.VENDOR_CONTRACT_TERMINATED,
    FinanceAuditAction.CONTRACT_MILESTONE_COMPLETED,
    FinanceAuditAction.PURCHASE_ORDER_APPROVED,
    FinanceAuditAction.GRN_POSTED,
    FinanceAuditAction.VENDOR_INVOICE_MATCHED,
    FinanceAuditAction.VENDOR_INVOICE_APPROVED,
    FinanceAuditAction.VENDOR_INVOICE_POSTED,
    FinanceAuditAction.VENDOR_PAYMENT_POSTED,
    FinanceAuditAction.VENDOR_PAYMENT_ALLOCATED,
    FinanceAuditAction.STOCK_RECEIVED,
    FinanceAuditAction.STOCK_ISSUED,
    FinanceAuditAction.STOCK_ADJUSTED,
)


# --------------------------------------------------------------------------- #
# Date/money helpers                                                          #
# --------------------------------------------------------------------------- #

def _money(kobo: int) -> dict:
    # API money is always integer minor units; the formatted value is display-only.
    value = int(kobo or 0)
    return {"kobo": value, "naira": format_naira(value)}


def _month_start(day: datetime.date) -> datetime.date:
    return day.replace(day=1)


def _shift_month(day: datetime.date, offset: int) -> datetime.date:
    # Flatten year/month to a zero-based month index so offsets cross year boundaries.
    absolute = day.year * 12 + day.month - 1 + offset
    # divmod-style arithmetic converts the absolute index back to year/month.
    return datetime.date(absolute // 12, absolute % 12 + 1, 1)


def _month_end(day: datetime.date) -> datetime.date:
    # calendar.monthrange handles leap years when selecting the final day.
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _spend_kobo(entity, start: datetime.date, end: datetime.date, branch_filter) -> int:
    """Sum realised gross spend in the inclusive invoice-date window."""
    # Spend is recognised only when a vendor invoice is posted, never while draft.
    return int(
        VendorInvoice.objects.filter(
            entity=entity,
            status=DocumentStatus.POSTED,
            invoice_date__range=(start, end),
        ).filter(branch_filter).aggregate(total=Sum("total"))["total"]
        or 0
    )


def _delta_pct(current: int, prior: int) -> float | None:
    """Return one-decimal period change, or ``None`` for a zero denominator."""
    if not prior:
        # A zero prior period has no meaningful percentage denominator.
        return None
    # Percentage change = (current - comparable prior) / comparable prior × 100.
    return round((current - prior) / prior * 100, 1)


# --------------------------------------------------------------------------- #
# Chart aggregates                                                            #
# --------------------------------------------------------------------------- #

def _category_items(report) -> dict:
    """The five largest categories of a spend analysis plus one exact long-tail slice."""
    top = report.by_category[:5]
    remainder = report.by_category[5:]
    items = [{"key": row.key, "label": row.label, "amount": _money(row.gross)} for row in top]
    if remainder:
        items.append({
            "key": "OTHER",
            "label": "Other",
            # Other is the exact sum of every category outside the top five.
            "amount": _money(sum(row.gross for row in remainder)),
        })
    return {"total": _money(report.total_gross), "items": items}


def _committed_vs_spent(entity, fiscal_start: datetime.date, as_of: datetime.date, branch_filter) -> dict:
    """Twelve months from the fiscal year's start: orders raised against bills posted.

    ``current`` is the index of ``as_of``'s month, or ``None`` outside the year.

    Committed is the value of issued purchase orders by order date (drafts, orders
    still in approval and cancelled orders are not commitments); spent is posted
    vendor bills by invoice date.
    """
    starts = [_shift_month(fiscal_start, offset) for offset in range(12)]
    end = _month_end(starts[-1])

    def by_month(qs, date_field, amount_field):
        return {
            (row["month"].date() if hasattr(row["month"], "date") else row["month"]): int(row["total"] or 0)
            for row in qs.filter(**{f"{date_field}__gte": starts[0], f"{date_field}__lte": end})
            .filter(branch_filter).annotate(month=TruncMonth(date_field)).values("month")
            .annotate(total=Sum(amount_field)).order_by("month")
        }

    committed = by_month(
        PurchaseOrder.objects.filter(entity=entity)
        .exclude(status__in=(DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL,
                             DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
        .exclude(approval_state=ProcApprovalState.PENDING),
        "order_date", "total",
    )
    spent = by_month(
        VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED), "invoice_date", "total",
    )
    return {
        "labels": [start.strftime("%b") for start in starts],
        "current": next((i for i, s in enumerate(starts) if s == _month_start(as_of)), None),
        "committed": [committed.get(start, 0) for start in starts],
        "spent": [spent.get(start, 0) for start in starts],
    }


def _pipeline(entity, start, end, as_of, reader, branch_scope, branch_filter) -> dict:
    """Where every open document sits between asking for something and paying for it.

    Each stage needs the key of the list it summarises, and counts what that list
    would show the reader. Paid covers the window; every other stage is open now.
    """
    from .constants import RfqStatus
    from .models import RequestForQuotation
    from .reports import grir_aging
    from .settings import resolve_procurement_settings
    from .views.orders import purchase_order_summary

    stages = {}
    if reader.can("procurement.requisition.view"):
        live = PurchaseOrder.objects.exclude(status__in=(DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
        reqs = (
            PurchaseRequisition.objects.filter(entity=entity).filter(branch_filter)
            .filter(status__in=(DocumentStatus.PENDING_APPROVAL, DocumentStatus.APPROVED))
            .exclude(purchase_orders__in=live)
        )
        agg = reqs.aggregate(n=Count("id", distinct=True), amount=Sum("estimated_total"),
                             waiting=Count("id", distinct=True, filter=Q(status=DocumentStatus.PENDING_APPROVAL)))
        stages["requisitions"] = {"count": agg["n"], "amount": _money(agg["amount"]), "flag": agg["waiting"]}
    if reader.can("procurement.rfq.view"):
        soon = resolve_procurement_settings(entity).rfq_closing_soon_days
        agg = RequestForQuotation.objects.filter(entity=entity, rfq_status=RfqStatus.ISSUED).filter(
            branch_filter,
        ).aggregate(n=Count("id"), amount=Sum("budget_estimate"), closing=Count("id", filter=Q(
            response_due_date__gte=as_of, response_due_date__lte=as_of + datetime.timedelta(days=soon))))
        stages["rfqs"] = {"count": agg["n"], "amount": _money(agg["amount"]), "flag": agg["closing"],
                          "flag_days": soon}
    if reader.can("procurement.purchase_order.view"):
        summary = purchase_order_summary(entity, as_of=as_of, branch_filter=branch_filter)
        stages["orders"] = {"count": summary["open"]["count"], "amount": _money(summary["open"]["amount"]),
                            "flag": summary["partially_received"]["count"]}
    if reader.can("procurement.goods_receipt.view"):
        open_rows = [r for r in grir_aging(entity, as_of=as_of, branch_scope=branch_scope).rows if r.open_value > 0]
        stages["received_not_billed"] = {
            "count": len(open_rows), "amount": _money(sum(r.open_value for r in open_rows)),
            "flag": sum(1 for r in open_rows if r.days > UNBILLED_RECEIPT_DAYS),
        }
    if reader.can("procurement.vendor_invoice.view"):
        open_bills = (
            VendorInvoice.objects.filter(entity=entity, status=DocumentStatus.POSTED).filter(branch_filter)
            .exclude(payment_status=InvoicePaymentStatus.PAID)
        )
        agg = open_bills.aggregate(n=Count("id"), amount=Sum(F("total") - F("amount_paid")),
                                   late=Count("id", filter=Q(due_date__lt=as_of)))
        stages["bills"] = {"count": agg["n"], "amount": _money(agg["amount"]), "flag": agg["late"]}
    if reader.can("procurement.vendor_payment.view"):
        agg = VendorPayment.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, payment_date__gte=start, payment_date__lte=end,
        ).filter(branch_filter).aggregate(n=Count("id"), amount=Sum("gross_amount"),
                                          vendors=Count("vendor_id", distinct=True))
        stages["paid"] = {"count": agg["n"], "amount": _money(agg["amount"]), "flag": agg["vendors"]}
    return stages


def _exceptions(entity, as_of, reader, branch_scope, branch_filter) -> list:
    """Controls that failed or are waiting on someone: only the ones with something in them."""
    from .constants import MatchStatus
    from .reports import grir_aging

    out = []
    if reader.can("procurement.vendor_invoice.view"):
        bills = VendorInvoice.objects.filter(entity=entity).filter(branch_filter).exclude(
            status__in=(DocumentStatus.CANCELLED, DocumentStatus.REVERSED),
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
        mismatch = bills.filter(match_status__in=(MatchStatus.UNDER_RECEIVED, MatchStatus.OVER_BILLED)).aggregate(
            n=Count("id"), amount=Sum("total"))
        if mismatch["n"]:
            out.append({"key": "match_failed", "count": mismatch["n"], "amount": _money(mismatch["amount"])})
        variance = bills.filter(match_status=MatchStatus.PRICE_VARIANCE).select_related("vendor")
        agg = variance.aggregate(n=Count("id"), amount=Sum("total"))
        if agg["n"]:
            first = variance.order_by("-total").first()
            out.append({"key": "price_variance", "count": agg["n"], "amount": _money(agg["amount"]),
                        "detail": first.vendor.name})
        held = bills.filter(status=DocumentStatus.POSTED, vendor__on_hold=True)
        agg = held.aggregate(n=Count("id"), amount=Sum(F("total") - F("amount_paid")))
        if agg["n"]:
            out.append({"key": "vendor_on_hold", "count": agg["n"], "amount": _money(agg["amount"]),
                        "detail": held.select_related("vendor").first().vendor.name})
    if reader.can("procurement.goods_receipt.view"):
        stale = [r for r in grir_aging(entity, as_of=as_of, branch_scope=branch_scope).rows
                 if r.open_value > 0 and r.days > UNBILLED_RECEIPT_DAYS]
        if stale:
            out.append({"key": "unbilled_receipts", "count": len(stale),
                        "amount": _money(sum(r.open_value for r in stale)), "days": UNBILLED_RECEIPT_DAYS})
    return out


def _bills_due(entity, as_of, branch_scope) -> dict:
    """Unpaid vendor bills by how late they are, from the AP aging report."""
    from .reports import ap_aging

    buckets = ap_aging(entity, as_of=as_of, branch_scope=branch_scope).bucket_totals
    return {
        "items": [
            {"key": "current", "amount": _money(buckets.get("current", 0))},
            {"key": "1-30", "amount": _money(buckets.get("1-30", 0))},
            {"key": "31-60", "amount": _money(buckets.get("31-60", 0))},
            {"key": "over-60", "amount": _money(buckets.get("61-90", 0) + buckets.get("90+", 0))},
        ],
    }


def _contracts_ending(entity, as_of, narrowed) -> list:
    """Active contracts ending within 90 days, with how much has been ordered against each.

    A contract is the school's, not a branch's, so the ordered figure (every
    branch's orders on it) is left out for a reader who sees only some branches.
    """
    from .constants import ContractStatus
    from .models import VendorContract

    ending = list(
        VendorContract.objects.filter(
            entity=entity, status=ContractStatus.ACTIVE,
            end_date__gte=as_of, end_date__lte=as_of + datetime.timedelta(days=CONTRACT_HORIZON_DAYS),
        ).select_related("vendor").order_by("end_date")[:CONTRACTS_SHOWN]
    )
    used = {} if narrowed else dict(
        PurchaseOrder.objects.filter(contract__in=ending)
        .exclude(status__in=(DocumentStatus.DRAFT, DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
        .values("contract_id").annotate(total=Sum("total")).values_list("contract_id", "total")
    )
    return [
        {
            "id": c.id, "title": c.title, "vendor": c.vendor.name, "end_date": c.end_date.isoformat(),
            "days": (c.end_date - as_of).days, "value": _money(c.contract_value),
            "ordered": None if narrowed else _money(used.get(c.id, 0)),
        }
        for c in ending
    ]


def _top_vendors(report) -> list:
    return [{"key": row.key, "name": row.label, "amount": _money(row.gross), "bills": row.invoice_count}
            for row in report.by_vendor[:TOP_VENDORS]]


def _requester_name(user) -> str:
    """Choose a display-safe actor label without exposing the user record."""
    if user is None:
        return "System"
    return (
        # Prefer the richest safe display name and fall back to the login identifier.
        getattr(user, "full_name", "")
        or user.get_full_name()
        or getattr(user, "email", "")
        or "Unknown user"
    )


def _pending_approvals(entity, user, branch_filter) -> list:
    """Build the actor's current procurement queue, constrained to ``entity`` and branch.

    This returns the complete de-duplicated queue; the dashboard composer owns the
    four-card presentation cap so the count KPI can still report the full queue.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []

    from vs_workflow.models import WorkflowStageAction, WorkflowStageApprover

    snaps = list(
        WorkflowStageApprover.objects.filter(
            user=user,
            # Ignore stale approver snapshots from a previous workflow attempt.
            attempt=F("stage_instance__attempt"),
            stage_instance__status="ACTIVE",
            stage_instance__instance__status="IN_PROGRESS",
            stage_instance__instance__document_type__in=PROCUREMENT_APPROVAL_TYPES,
        )
        .select_related(
            "stage_instance__instance__requested_by",
            "stage_instance__instance__current_stage",
            "stage_instance__instance__document_content_type",
        )
        .order_by("-stage_instance__activated_at")
    )
    # A stage/attempt already acted by this user must not reappear as pending.
    # Bound the scan to the stages actually in play (not the user's whole action
    # history) so the query cost stays proportional to the pending queue.
    stage_ids = {snap.stage_instance_id for snap in snaps}
    acted = set(
        WorkflowStageAction.objects.filter(
            actor=user,
            stage_instance_id__in=stage_ids,
            reversed_at__isnull=True,
            is_reversal_of__isnull=True,
        ).values_list("stage_instance_id", "attempt")
    ) if stage_ids else set()

    models = {
        # Workflow stores generic object ids; map each allowed type to its real model.
        WF_DOCTYPE_REQUISITION: PurchaseRequisition,
        WF_DOCTYPE_PURCHASE_ORDER: PurchaseOrder,
        WF_DOCTYPE_VENDOR_INVOICE: VendorInvoice,
        WF_DOCTYPE_VENDOR_PAYMENT: VendorPayment,
    }
    ids_by_type: dict[str, set[int]] = {key: set() for key in models}
    usable = []
    seen_instances = set()
    for snap in snaps:
        stage = snap.stage_instance
        instance = stage.instance
        # Remove completed votes and duplicate snapshots for the same workflow instance.
        if (stage.id, stage.attempt) in acted or instance.id in seen_instances:
            continue
        try:
            object_id = int(instance.document_object_id)
        except (TypeError, ValueError):
            continue
        # Collect ids first so each document type is loaded in one entity-scoped query.
        ids_by_type[instance.document_type].add(object_id)
        usable.append((snap, object_id))
        seen_instances.add(instance.id)

    documents = {
        # Filtering by entity here is the cross-tenant boundary for generic workflows;
        # the branch filter is the sub-scope, so a card can never name a document the
        # caller is not allowed to open.
        doc_type: {
            row.pk: row
            for row in model.objects.filter(
                entity=entity, pk__in=ids_by_type[doc_type],
            ).filter(branch_filter).select_related(
                *("vendor",) if doc_type != WF_DOCTYPE_REQUISITION else ()
            )
        }
        for doc_type, model in models.items()
    }

    items = []
    for snap, object_id in usable:
        stage = snap.stage_instance
        instance = stage.instance
        document = documents[instance.document_type].get(object_id)
        if document is None:  # The workflow target belongs to another ledger entity.
            continue
        # Each document declares which money field the workflow should display.
        amount_field = getattr(document, "workflow_amount_field", "")
        amount = int(getattr(document, amount_field, 0) or 0)
        vendor = getattr(document, "vendor", None)
        # Use only persisted display fields; never expose the workflow metadata bag.
        title = (
            getattr(document, "justification", "")
            or (getattr(vendor, "name", "") if vendor else "")
            or dict(instance.document_summary or {}).get("subtitle", "")
        )
        items.append({
            "workflow_id": instance.id,
            "document_type": instance.document_type,
            "document_id": object_id,
            "reference": document.document_number or str(document.pk),
            "title": title,
            "requester": _requester_name(instance.requested_by),
            "amount": _money(amount),
            "stage": getattr(instance.current_stage, "label", "") or "Approval",
            "awaiting_since": stage.activated_at.isoformat() if stage.activated_at else None,
            "on_behalf_of": str(snap.on_behalf_of_id) if snap.on_behalf_of_id else None,
        })
    return items


def _recent_activity(entity) -> list:
    """Return the five newest successful procurement audit events for ``entity``."""
    rows = (
        FinanceAuditLog.objects.filter(
            entity=entity,
            action__in=PROCUREMENT_AUDIT_ACTIONS,
            # Failed attempts belong in audit, but not in a completed-activity feed.
            status=FinanceAuditStatus.SUCCESS,
        )
        .select_related("actor")
        # The Dashboard intentionally shows at most the five newest successful events.
        .order_by("-created_at", "-id")[:5]
    )
    return [
        {
            "id": row.pk,
            "action": row.action,
            "label": row.get_action_display(),
            # Finance audit messages occasionally contain raw kobo for audit-grade
            # precision. Keep the dashboard display human-readable and leave the raw
            # message inside the protected audit trail.
            "summary": (
                f"{row.get_action_display()} · {row.document_number}"
                if row.document_number else row.get_action_display()
            ),
            "reference": row.document_number,
            "actor": _requester_name(row.actor),
            "occurred_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def procurement_dashboard(entity, *, user=None, as_of: datetime.date | None = None,
                          branch_scope=None, reader=None, window: str | None = None) -> dict:
    """Return the Procurement overview payload for one ledger entity.

    ``window`` picks the span the spend, category, top-vendor and paid figures
    read (the finance dashboard's windows: this month, a school's term, the year
    to date), always by date. Spend is compared with the same number of days of
    the window before. Everything else is open right now.

    ``branch_scope`` is the caller's branch narrowing (``views.base._BranchScope``,
    the same object the analytics reports take), applied to every
    document-derived figure so a branch-bound viewer's figures match the lists
    they can open. ``active_vendors`` and contracts stay entity-wide because they
    are master data every branch shares; a contract's ordered value is withheld
    from a narrowed reader, since it sums every branch's orders.

    ``reader`` (:class:`vs_finance.dashboard.DashboardReader`) decides which blocks
    are computed at all: spend, categories, top vendors and committed-vs-spent
    need ``procurement.analytics.view``; each pipeline stage needs the key of
    the list it summarises; exceptions and bills falling due need
    ``procurement.vendor_invoice.view`` (unbilled receipts,
    ``procurement.goods_receipt.view``); contracts need
    ``procurement.contract.view``; the vendor count ``procurement.vendor.view``.
    A block the reader may not see is ``None``. The approval queue is the
    reader's own and always present. The activity feed cannot be narrowed by
    branch, so it goes only to an analytics reader whose reach is the whole
    school. ``None`` means every block, which is what callers without a request
    get.
    """
    from django.db.models import Min

    from vs_finance.dashboard import EVERY_BLOCK, _current_period
    from vs_finance.dashboard_blocks import books_kind, resolve_window
    from vs_finance.dashboard_spend import same_point_before, window_days

    from .reports import spend_analysis

    reader = reader or EVERY_BLOCK
    narrowed = branch_scope is not None and branch_scope.is_narrowed
    analytics = reader.can("procurement.analytics.view")
    orders = reader.can("procurement.purchase_order.view")
    bills = reader.can("procurement.vendor_invoice.view")
    vendors = reader.can("procurement.vendor.view")
    as_of = as_of or timezone.localdate()
    current = _current_period(entity, None)
    chosen, windows = resolve_window(entity, as_of, current, window)
    start, end = window_days(chosen, as_of)
    fiscal = getattr(current, "fiscal_year", None)
    fiscal_start = fiscal.start_date if fiscal is not None else as_of.replace(month=1, day=1)

    # Every block below reads models that carry ``branch`` directly, so the scope
    # collapses to a single Q here.
    branch_filter = branch_scope.q() if branch_scope is not None else Q()

    approvals = _pending_approvals(entity, user, branch_filter)
    slow_before = timezone.now() - datetime.timedelta(days=SLOW_APPROVAL_DAYS)

    spend_kpi = spend = None
    if analytics:
        spend = spend_analysis(entity, start_date=start, end_date=end, branch_scope=branch_scope)
        before = same_point_before(entity, chosen, as_of)
        previous = _spend_kobo(entity, *before, branch_filter) if before else None
        spend_kpi = {
            "value": _money(spend.total_gross),
            "prior_value": _money(previous) if previous is not None else None,
            "delta_pct": _delta_pct(spend.total_gross, previous or 0),
        }
    open_orders = None
    if orders:
        from .views.orders import purchase_order_summary

        summary = purchase_order_summary(entity, as_of=as_of, branch_filter=branch_filter)
        open_orders = {"count": summary["open"]["count"], "partial_count": summary["partially_received"]["count"],
                       "amount": _money(summary["open"]["amount"])}

    overdue_kpi = None
    if bills:
        # Strictly earlier due dates are overdue; an invoice due on as_of remains current.
        overdue_values = VendorInvoice.objects.filter(
            entity=entity, status=DocumentStatus.POSTED, due_date__lt=as_of,
        ).filter(branch_filter).exclude(payment_status=InvoicePaymentStatus.PAID).aggregate(
            count=Count("id"),
            # Outstanding balance is invoice total less all allocations already paid.
            amount=Sum(F("total") - F("amount_paid")),
            oldest=Min("due_date"),
        )
        overdue_kpi = {
            "count": int(overdue_values["count"] or 0),
            "amount": _money(int(overdue_values["amount"] or 0)),
            "oldest_days": (as_of - overdue_values["oldest"]).days if overdue_values["oldest"] else None,
        }
    vendors_kpi = None
    if vendors:
        active_vendors = Vendor.objects.filter(entity=entity, is_active=True)
        first_orders = (
            PurchaseOrder.objects.filter(entity=entity)
            .exclude(status__in=(DocumentStatus.DRAFT, DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
            .values("vendor_id").annotate(first=Min("order_date"))
        )
        vendors_kpi = {
            "count": active_vendors.count(),
            "on_hold_count": active_vendors.filter(on_hold=True).count(),
            "first_time_count": sum(1 for row in first_orders if start <= row["first"] <= end),
        }

    return {
        "entity": entity.code,
        "currency": entity.base_currency_id,
        "books": books_kind(entity),
        "reader_first_name": (getattr(user, "first_name", "") or "").strip() or None,
        "as_of": as_of.isoformat(),
        "month_start": _month_start(as_of).isoformat(),
        "narrowed": narrowed,
        "window": {**chosen.payload(), "basis": "dates"},
        "windows": [{"key": w.key, "label": w.label, "name": w.name} for w in windows],
        "kpis": {
            "spend": spend_kpi,
            "open_purchase_orders": open_orders,
            "pending_approvals": {
                "count": len(approvals),
                "amount": _money(sum(a["amount"]["kobo"] for a in approvals)),
                "slow_count": sum(
                    1 for a in approvals
                    if a["awaiting_since"] and datetime.datetime.fromisoformat(a["awaiting_since"]) < slow_before
                ),
                "type_count": len({a["document_type"] for a in approvals}),
            },
            "overdue_invoices": overdue_kpi,
            "active_vendors": vendors_kpi,
        },
        "pipeline": _pipeline(entity, start, end, as_of, reader, branch_scope, branch_filter),
        "committed_vs_spent": _committed_vs_spent(entity, fiscal_start, as_of, branch_filter) if analytics else None,
        "spend_by_category": _category_items(spend) if spend is not None else None,
        "top_vendors": _top_vendors(spend) if spend is not None else None,
        "exceptions": _exceptions(entity, as_of, reader, branch_scope, branch_filter)
        if bills or reader.can("procurement.goods_receipt.view") else None,
        "bills_due": _bills_due(entity, as_of, branch_scope) if bills else None,
        "contracts_ending": (
            _contracts_ending(entity, as_of, narrowed) if reader.can("procurement.contract.view") else None
        ),
        # The finance audit log carries no branch column, so the feed cannot be
        # narrowed; a branch-bound reader does not get it.
        "recent_activity": _recent_activity(entity) if analytics and not narrowed else None,
        "approvals_awaiting_user": approvals[:APPROVALS_SHOWN],
    }
