"""AP reports and procurement analytics.
"""
from __future__ import annotations



from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity



from .base import (
    _kobo,
    _ProcBase,
    _date,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# AP reports                                                                  #
# --------------------------------------------------------------------------- #

class APAgingView(_ProcBase):
    """docstring-name: AP aging report"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import AGING_BUCKETS, ap_aging

        entity = resolve_entity(request)
        # Parse ``as_of`` to a date: ap_aging computes ``as_of - due_date``, so a raw
        # query-string would raise TypeError (str − date) and 500 the request.
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = ap_aging(entity, as_of=as_of)
        return success_response(
            "AP aging retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                        "payment_terms": r.payment_terms,
                        "buckets": {b: _kobo(v) for b, v in r.buckets.items()},
                        "outstanding": _kobo(r.outstanding),
                        "unallocated_credit": _kobo(r.unallocated_credit),
                        "net": _kobo(r.net),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_net": _kobo(report.total_net),
            },
        )


class APReconciliationView(_ProcBase):
    """docstring-name: AP reconciliation report"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import reconcile_ap

        entity = resolve_entity(request)
        # Parse ``as_of`` to a date: reconcile_ap → ap_aging does ``as_of - due_date``,
        # so a raw query-string would raise TypeError (str − date) and 500 the request.
        as_of = _date(request.query_params.get("as_of"), "as_of")
        rec = reconcile_ap(entity, as_of=as_of)
        return success_response(
            "AP reconciliation retrieved.",
            data={
                "entity": entity.code,
                "subledger_total": _kobo(rec.subledger_total),
                "control_total": _kobo(rec.control_total),
                "difference": _kobo(rec.difference),
                "is_reconciled": rec.is_reconciled,
            },
        )


class GRIRBalanceView(_ProcBase):
    """docstring-name: GR/IR balance"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import grir_balance

        entity = resolve_entity(request)
        balance = grir_balance(entity)
        return success_response(
            "GR/IR clearing balance retrieved.",
            data={
                "entity": entity.code,
                "grir_balance": _kobo(balance),
                "is_clear": balance == 0,
            },
        )


class APCashRequirementsView(_ProcBase):
    """docstring-name: AP cash requirements"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import FORECAST_BUCKETS, ap_cash_requirements

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = ap_cash_requirements(entity, as_of=as_of)
        return success_response(
            "AP cash-requirements forecast retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(FORECAST_BUCKETS),
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                        "buckets": {b: _kobo(v) for b, v in r.buckets.items()},
                        "total": _kobo(r.total),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_due": _kobo(report.total_due),
            },
        )


class GRIRAgingView(_ProcBase):
    """docstring-name: GR/IR aging report"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import AGING_BUCKETS, grir_aging

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = grir_aging(entity, as_of=as_of)
        return success_response(
            "GR/IR aging retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "rows": [
                    {
                        "grn_id": r.grn_id, "reference": r.reference,
                        "vendor_code": r.vendor_code, "vendor_name": r.vendor_name,
                        "received_date": str(r.received_date), "days": r.days,
                        "bucket": r.bucket,
                        "received_value": _kobo(r.received_value),
                        "invoiced_value": _kobo(r.invoiced_value),
                        "open_value": _kobo(r.open_value),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_open": _kobo(report.total_open),
                "control_balance": _kobo(report.control_balance),
                "difference": _kobo(report.difference),
            },
        )


class APAgingVendorDetailView(_ProcBase):
    """docstring-name: AP aging — vendor detail

    Per-vendor AP drawer: aging buckets + the vendor's open POSTED bills. Report-gated
    so a report viewer can open it without holding ``vendor_invoice.view``.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import AGING_BUCKETS, ap_vendor_open_bills

        entity = resolve_entity(request)
        # Entity-scoped vendor resolution — a foreign vendor 404s rather than leaking.
        vendor = _resolve_vendor(entity, request.query_params.get("vendor"))
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = ap_vendor_open_bills(entity, vendor, as_of=as_of)
        return success_response(
            "Vendor AP detail retrieved.",
            data={
                "entity": entity.code, "as_of": str(detail.as_of),
                "buckets": list(AGING_BUCKETS),
                "vendor": {"id": detail.vendor_id, "code": detail.code, "name": detail.name},
                "bucket_amounts": {b: _kobo(v) for b, v in detail.buckets.items()},
                "outstanding": _kobo(detail.outstanding),
                "unallocated_credit": _kobo(detail.unallocated_credit),
                "net": _kobo(detail.net),
                "invoices": [
                    {
                        "invoice_id": inv.invoice_id, "document_number": inv.document_number,
                        "invoice_date": str(inv.invoice_date),
                        "due_date": str(inv.due_date) if inv.due_date else None,
                        "days_overdue": inv.days_overdue, "bucket": inv.bucket,
                        "balance_due": _kobo(inv.balance_due),
                        "payment_status": inv.payment_status,
                    }
                    for inv in detail.invoices
                ],
            },
        )


class GRIRGrnDetailView(_ProcBase):
    """docstring-name: GR/IR aging — GRN detail

    Per-GRN GR/IR drawer: the reconciliation figures + linked PO and matched invoices.
    Report-gated so a report viewer can open it without holding ``goods_receipt.view``.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import grir_grn_detail

        entity = resolve_entity(request)
        grn_ref = request.query_params.get("grn")
        if not grn_ref or not str(grn_ref).isdigit():
            raise ValidationError({"grn": "A numeric GRN id is required."})
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = grir_grn_detail(entity, int(grn_ref), as_of=as_of)
        if detail is None:
            raise NotFound("No such goods-received note in this entity.")
        return success_response(
            "GR/IR GRN detail retrieved.",
            data={
                "entity": entity.code,
                "grn_id": detail.grn_id, "reference": detail.reference,
                "vendor_code": detail.vendor_code, "vendor_name": detail.vendor_name,
                "received_date": str(detail.received_date),
                "days": detail.days, "bucket": detail.bucket,
                "po_number": detail.po_number or None,
                "received_value": _kobo(detail.received_value),
                "invoiced_value": _kobo(detail.invoiced_value),
                "open_value": _kobo(detail.open_value),
                "invoices": [
                    {
                        "id": vi["id"], "document_number": vi["document_number"],
                        "invoice_date": vi["invoice_date"], "net": _kobo(vi["net"]),
                    }
                    for vi in detail.invoices
                ],
            },
        )


class GRIRPoLinesView(_ProcBase):
    """docstring-name: GR/IR PO-line report

    Line-grain GR/IR reconciliation: per live PO line, ordered vs received vs invoiced
    (quantity + kobo value) with a derived Cleared / Received>Invoiced / Invoiced>Received
    status. Feeds the prototype's PO-line GR/IR table. Report-gated, entity-scoped.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import grir_po_lines

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = grir_po_lines(entity, as_of=as_of)
        return success_response(
            "GR/IR PO-line report retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "rows": [
                    {
                        "po_line_id": r.po_line_id, "po_line_ref": r.po_line_ref,
                        "item": r.item,
                        "vendor_code": r.vendor_code, "vendor_name": r.vendor_name,
                        "ordered_qty": r.ordered_qty, "received_qty": r.received_qty,
                        "invoiced_qty": r.invoiced_qty,
                        "received_value": _kobo(r.received_value),
                        "invoiced_value": _kobo(r.invoiced_value),
                        "grir_balance": _kobo(r.grir_balance),
                        "status": r.status,
                    }
                    for r in report.rows
                ],
            },
        )


class GRIRPoLineDetailView(_ProcBase):
    """docstring-name: GR/IR PO-line detail

    Per-PO-line GR/IR drawer: the reconciliation figures + the linked POSTED goods
    receipts and vendor invoices. Report-gated so a report viewer can open it without
    holding purchase_order/goods_receipt view keys; a foreign PO-line id 404s.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import grir_po_line_detail

        entity = resolve_entity(request)
        line_ref = request.query_params.get("po_line")
        if not line_ref or not str(line_ref).isdigit():
            raise ValidationError({"po_line": "A numeric PO-line id is required."})
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = grir_po_line_detail(entity, int(line_ref), as_of=as_of)
        if detail is None:
            raise NotFound("No such purchase-order line in this entity.")
        return success_response(
            "GR/IR PO-line detail retrieved.",
            data={
                "entity": entity.code,
                "po_line_id": detail.po_line_id, "po_line_ref": detail.po_line_ref,
                "item": detail.item,
                "vendor_code": detail.vendor_code, "vendor_name": detail.vendor_name,
                "po_number": detail.po_number,
                "ordered_qty": detail.ordered_qty, "received_qty": detail.received_qty,
                "invoiced_qty": detail.invoiced_qty,
                "received_value": _kobo(detail.received_value),
                "invoiced_value": _kobo(detail.invoiced_value),
                "grir_balance": _kobo(detail.grir_balance),
                "status": detail.status, "unit_price": _kobo(detail.unit_price),
                "grns": [
                    {
                        "id": g["id"], "reference": g["reference"],
                        "received_date": g["received_date"],
                        "accepted_qty": g["accepted_qty"], "value": _kobo(g["value"]),
                    }
                    for g in detail.grns
                ],
                "invoices": [
                    {
                        "id": vi["id"], "document_number": vi["document_number"],
                        "invoice_date": vi["invoice_date"],
                        "quantity": vi["quantity"], "net": _kobo(vi["net"]),
                    }
                    for vi in detail.invoices
                ],
            },
        )


# --------------------------------------------------------------------------- #
# Procurement analytics                                                        #
# --------------------------------------------------------------------------- #

class ProcurementDashboardView(_ProcBase):
    """docstring-name: Procurement dashboard"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..dashboard import procurement_dashboard

        entity = resolve_entity(request)
        return success_response(
            "Procurement dashboard retrieved.",
            data=procurement_dashboard(entity, user=request.user),
        )


class SpendAnalysisView(_ProcBase):
    """docstring-name: Spend analysis"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import spend_analysis

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        # Optional ?category=<code|UNCATEGORISED> scopes the whole report to one category.
        category = request.query_params.get("category") or None
        report = spend_analysis(entity, start_date=start, end_date=end, category=category)

        def _rows(rows):
            return [
                {
                    "key": r.key, "label": r.label,
                    "net": _kobo(r.net), "tax": _kobo(r.tax), "gross": _kobo(r.gross),
                    "invoice_count": r.invoice_count,
                }
                for r in rows
            ]

        return success_response(
            "Spend analysis retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "category": category,
                "by_vendor": _rows(report.by_vendor),
                "by_category": _rows(report.by_category),
                "by_period": [
                    {
                        "period": p.period, "label": p.label,
                        "gross": _kobo(p.gross), "invoice_count": p.invoice_count,
                    }
                    for p in report.by_period
                ],
                "total_net": _kobo(report.total_net),
                "total_tax": _kobo(report.total_tax),
                "total_gross": _kobo(report.total_gross),
                "invoice_count": report.invoice_count,
            },
        )


class VendorPerformanceView(_ProcBase):
    """docstring-name: Vendor performance"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import vendor_performance

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        report = vendor_performance(entity, start_date=start, end_date=end)
        return success_response(
            "Vendor performance retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                        "category": r.category,
                        "po_count": r.po_count, "total_ordered": _kobo(r.total_ordered),
                        "receipt_count": r.receipt_count,
                        "on_time_receipts": r.on_time_receipts,
                        "late_receipts": r.late_receipts,
                        "on_time_rate": r.on_time_rate,
                        "invoice_count": r.invoice_count,
                        "total_billed": _kobo(r.total_billed),
                        "payment_count": r.payment_count,
                        "total_paid": _kobo(r.total_paid),
                        "avg_payment_days": r.avg_payment_days,
                        # Recorded scorecard (or null). On-time stays the COMPUTED
                        # on_time_rate above — the assessment never overwrites it.
                        "latest_assessment": (
                            {
                                "quality_acceptance": a.quality_acceptance,
                                "invoice_accuracy": a.invoice_accuracy,
                                "responsiveness": a.responsiveness,
                                "overall_score": a.overall_score,
                                "grade": a.grade,
                                "assessment_date": str(a.assessment_date),
                            } if (a := r.latest_assessment) else None
                        ),
                    }
                    for r in report.rows
                ],
            },
        )


class ProcurementCycleTimeView(_ProcBase):
    """docstring-name: Procurement cycle time"""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from ..reports import procurement_cycle_time

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        report = procurement_cycle_time(entity, start_date=start, end_date=end)
        return success_response(
            "Procurement cycle time retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "stages": [
                    {
                        "name": s.name, "label": s.label,
                        "sample_count": s.sample_count, "avg_days": s.avg_days,
                    }
                    for s in report.stages
                ],
                "end_to_end_avg_days": report.end_to_end_avg_days,
                "end_to_end_count": report.end_to_end_count,
            },
        )

