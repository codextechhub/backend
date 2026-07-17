"""Entity-scoped aggregate for the Procurement landing dashboard.

The dashboard is deliberately assembled on the server: list endpoints are paginated,
workflow approvals are actor-specific, and all blocks need to share one ``as_of``
snapshot.  Only display-safe fields leave this module; raw audit/workflow metadata is
never exposed.
"""
from __future__ import annotations

import calendar
import datetime
from collections import Counter
from decimal import Decimal

from django.db.models import Count, F, Sum
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
    ProcApprovalState,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
)
from .models import PurchaseOrder, Vendor, VendorInvoice
from .reports import spend_analysis


PROCUREMENT_APPROVAL_TYPES = (
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_VENDOR_INVOICE,
)

PROCUREMENT_AUDIT_ACTIONS = (
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


def _money(kobo: int) -> dict:
    value = int(kobo or 0)
    return {"kobo": value, "naira": format_naira(value)}


def _month_start(day: datetime.date) -> datetime.date:
    return day.replace(day=1)


def _shift_month(day: datetime.date, offset: int) -> datetime.date:
    absolute = day.year * 12 + day.month - 1 + offset
    return datetime.date(absolute // 12, absolute % 12 + 1, 1)


def _month_end(day: datetime.date) -> datetime.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _spend_kobo(entity, start: datetime.date, end: datetime.date) -> int:
    return int(
        VendorInvoice.objects.filter(
            entity=entity,
            status=DocumentStatus.POSTED,
            invoice_date__range=(start, end),
        ).aggregate(total=Sum("total"))["total"]
        or 0
    )


def _delta_pct(current: int, prior: int) -> float | None:
    if not prior:
        return None
    return round((current - prior) / prior * 100, 1)


def _po_status(entity) -> dict:
    """Keep chart stages aligned with the PO list; derive receipt KPIs separately."""
    rows = (
        PurchaseOrder.objects.filter(entity=entity)
        .exclude(status__in=(DocumentStatus.CANCELLED, DocumentStatus.REVERSED))
        .annotate(ordered_qty=Sum("lines__quantity"), received_qty=Sum("lines__received_qty"))
        .values("status", "approval_state", "ordered_qty", "received_qty")
    )
    counts = Counter({"DRAFT": 0, "PENDING": 0, "APPROVED": 0, "PARTIAL": 0, "CLOSED": 0})
    open_count = 0
    partial_count = 0
    for row in rows:
        ordered = Decimal(row["ordered_qty"] or 0)
        received = Decimal(row["received_qty"] or 0)
        if row["approval_state"] == ProcApprovalState.PENDING or row["status"] == DocumentStatus.PENDING_APPROVAL:
            counts["PENDING"] += 1
        elif row["status"] == DocumentStatus.DRAFT:
            counts["DRAFT"] += 1
        else:
            counts["APPROVED"] += 1

        # The PO model does not persist PARTIAL/CLOSED document statuses. Keep
        # those chart buckets at zero rather than contradicting the list, while
        # preserving the useful receipt-aware KPI calculation.
        if not (ordered > 0 and received >= ordered):
            open_count += 1
        if ordered > 0 and 0 < received < ordered:
            partial_count += 1
    return {
        "items": [
            {"key": key, "label": label, "count": counts[key]}
            for key, label in (
                ("APPROVED", "Approved"),
                ("PARTIAL", "Partial"),
                ("PENDING", "Pending"),
                ("DRAFT", "Draft"),
                ("CLOSED", "Closed"),
            )
        ],
        "open_count": open_count,
        "partial_count": partial_count,
    }


def _spend_by_category(entity, start: datetime.date, end: datetime.date) -> dict:
    report = spend_analysis(entity, start_date=start, end_date=end)
    top = report.by_category[:5]
    remainder = report.by_category[5:]
    items = [{"key": row.key, "label": row.label, "amount": _money(row.gross)} for row in top]
    if remainder:
        items.append({
            "key": "OTHER",
            "label": "Other",
            "amount": _money(sum(row.gross for row in remainder)),
        })
    return {"total": _money(report.total_gross), "items": items}


def _monthly_trend(entity, as_of: datetime.date) -> dict:
    starts = [_shift_month(_month_start(as_of), offset) for offset in range(-7, 1)]
    values = {
        (row["month"].date() if hasattr(row["month"], "date") else row["month"]): int(row["total"] or 0)
        for row in (
            VendorInvoice.objects.filter(
                entity=entity,
                status=DocumentStatus.POSTED,
                invoice_date__gte=starts[0],
                invoice_date__lte=as_of,
            )
            .annotate(month=TruncMonth("invoice_date"))
            .values("month")
            .annotate(total=Sum("total"))
            .order_by("month")
        )
    }
    return {
        "labels": [start.strftime("%b") for start in starts],
        "values": [values.get(start, 0) for start in starts],
    }


def _requester_name(user) -> str:
    if user is None:
        return "System"
    return (
        getattr(user, "full_name", "")
        or user.get_full_name()
        or getattr(user, "email", "")
        or "Unknown user"
    )


def _pending_approvals(entity, user) -> list:
    if user is None or not getattr(user, "is_authenticated", False):
        return []

    from vs_workflow.models import WorkflowStageAction, WorkflowStageApprover

    snaps = list(
        WorkflowStageApprover.objects.filter(
            user=user,
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
    acted = set(
        WorkflowStageAction.objects.filter(
            actor=user,
            reversed_at__isnull=True,
            is_reversal_of__isnull=True,
        ).values_list("stage_instance_id", "attempt")
    )

    models = {
        WF_DOCTYPE_REQUISITION: __import__("vs_procurement.models", fromlist=["PurchaseRequisition"]).PurchaseRequisition,
        WF_DOCTYPE_PURCHASE_ORDER: PurchaseOrder,
        WF_DOCTYPE_VENDOR_INVOICE: VendorInvoice,
    }
    ids_by_type: dict[str, set[int]] = {key: set() for key in models}
    usable = []
    seen_instances = set()
    for snap in snaps:
        stage = snap.stage_instance
        instance = stage.instance
        if (stage.id, stage.attempt) in acted or instance.id in seen_instances:
            continue
        try:
            object_id = int(instance.document_object_id)
        except (TypeError, ValueError):
            continue
        ids_by_type[instance.document_type].add(object_id)
        usable.append((snap, object_id))
        seen_instances.add(instance.id)

    documents = {
        doc_type: {
            row.pk: row
            for row in model.objects.filter(entity=entity, pk__in=ids_by_type[doc_type]).select_related(
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
        amount_field = getattr(document, "workflow_amount_field", "")
        amount = int(getattr(document, amount_field, 0) or 0)
        vendor = getattr(document, "vendor", None)
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
    rows = (
        FinanceAuditLog.objects.filter(
            entity=entity,
            action__in=PROCUREMENT_AUDIT_ACTIONS,
            status=FinanceAuditStatus.SUCCESS,
        )
        .select_related("actor")
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


def procurement_dashboard(entity, *, user=None, as_of: datetime.date | None = None) -> dict:
    """Return the complete Procurement Dashboard payload for one ledger entity."""
    as_of = as_of or timezone.localdate()
    current_start = _month_start(as_of)
    previous_start = _shift_month(current_start, -1)
    previous_end = min(
        _month_end(previous_start),
        previous_start + datetime.timedelta(days=as_of.day - 1),
    )

    current_spend = _spend_kobo(entity, current_start, as_of)
    previous_spend = _spend_kobo(entity, previous_start, previous_end)
    po_status = _po_status(entity)
    approvals = _pending_approvals(entity, user)

    overdue = VendorInvoice.objects.filter(
        entity=entity,
        status=DocumentStatus.POSTED,
        due_date__lt=as_of,
    ).exclude(payment_status=InvoicePaymentStatus.PAID)
    overdue_values = overdue.aggregate(
        count=Count("id"),
        amount=Sum(F("total") - F("amount_paid")),
    )
    active_vendors = Vendor.objects.filter(entity=entity, is_active=True)

    return {
        "entity": entity.code,
        "currency": entity.base_currency_id,
        "as_of": as_of.isoformat(),
        "month_start": current_start.isoformat(),
        "kpis": {
            "total_spend_mtd": {
                "value": _money(current_spend),
                "prior_value": _money(previous_spend),
                "delta_pct": _delta_pct(current_spend, previous_spend),
            },
            "open_purchase_orders": {
                "count": po_status["open_count"],
                "partial_count": po_status["partial_count"],
            },
            "pending_approvals": {"count": len(approvals)},
            "overdue_invoices": {
                "count": int(overdue_values["count"] or 0),
                "amount": _money(int(overdue_values["amount"] or 0)),
            },
            "active_vendors": {
                "count": active_vendors.count(),
                "on_hold_count": active_vendors.filter(on_hold=True).count(),
            },
        },
        "spend_by_category": _spend_by_category(entity, current_start, as_of),
        "purchase_order_status": {"items": po_status["items"]},
        "monthly_spend_trend": _monthly_trend(entity, as_of),
        "recent_activity": _recent_activity(entity),
        "approvals_awaiting_user": approvals[:4],
    }
