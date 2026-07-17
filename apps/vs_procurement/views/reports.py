"""AP reports and procurement analytics.
"""
from __future__ import annotations



from core.response import success_response
from vs_finance.views import resolve_entity



from .base import (
    _kobo,
    _ProcBase,
    _date,
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
        as_of = request.query_params.get("as_of") or None
        report = ap_aging(entity, as_of=as_of)
        return success_response(
            "AP aging retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
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
        as_of = request.query_params.get("as_of") or None
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
        report = spend_analysis(entity, start_date=start, end_date=end)

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
                "by_vendor": _rows(report.by_vendor),
                "by_category": _rows(report.by_category),
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

