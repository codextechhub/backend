"""Read-only AP reconciliation and procurement analytics adapters.

Views resolve the ledger entity before invoking report services, parse every date
filter into a real calendar date, and expose money as explicit ``{kobo, naira}``
objects.  Detail drawers stay report-gated and repeat entity scope so report access
does not imply broad access to the underlying operational resources.

Every report whose population is documents also repeats the caller's **branch** scope,
resolved by :func:`_scope` through the same helper the operational lists use, so a
branch-bound viewer's analytics reconcile with the documents they can actually open
instead of quietly reporting the whole tenant.  The two exceptions are the AP
reconciliation and the GR/IR balance: their subject is a general-ledger control account,
which has no branch column, and each says so on its own class.
"""
from __future__ import annotations



from rest_framework.exceptions import NotFound, ValidationError

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.views import resolve_entity



from .base import (
    _kobo,
    _ProcBase,
    _branch_scope,
    _date,
    _resolve_vendor,
)


def _validate_date_window(start, end):
    """Reject an inverted inclusive report window instead of returning a false empty."""
    if start is not None and end is not None and start > end:
        raise ValidationError({
            "end_date": "end_date must be on or after start_date.",
        })


def _scope(request, entity):
    """The branch narrowing this report must answer under.

    Reports are aggregates of the very documents the operational lists return, so they
    resolve their scope through the same helper those lists use rather than deciding
    again: a report that disagrees with the list beside it is worse than no report.
    The service then renders it per population, because one report can span several
    models that reach ``branch`` by different routes.
    """
    return _branch_scope(request, entity, request.query_params)


def _with_unassigned(data, report):
    """Add the excluded entity-level count, and only when the caller is narrowed.

    ``unassigned_excluded_count`` is ``None`` for an unbound caller and for a tenant with
    no branches, and the key is then absent entirely, so those callers keep byte-identical
    responses.  When present it names how many documents in this report's population sit
    at entity level (a null branch, typically raised before the column existed) and are
    therefore outside the caller's view - so a subset total is never mistaken for the
    whole picture.  A count, never an amount: another scope's money stays private.
    """
    count = report.unassigned_excluded_count
    if count is not None:
        data["unassigned_excluded_count"] = count
    return data


def _paginated_report(request, rows, data, *, key, render, message):
    """Page one primary list while retaining entity-wide report totals in ``data``."""
    paginator = XVSPagination()
    paginator.page_size = 25
    page = paginator.paginate_queryset(rows, request)
    data[key] = [render(row) for row in page]
    response = paginator.get_paginated_response(data)
    response.data["message"] = message
    return response

# --------------------------------------------------------------------------- #
# AP reports                                                                  #
# --------------------------------------------------------------------------- #

class APAgingView(_ProcBase):
    """Age posted open AP by vendor and due-date bucket."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return entity AP aging with explicit minor-unit money values."""
        from ..reports import AGING_BUCKETS, ap_aging

        entity = resolve_entity(request)
        # Parse ``as_of`` to a date: ap_aging computes ``as_of - due_date``, so a raw
        # query-string would raise TypeError (str − date) and 500 the request.
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = ap_aging(entity, as_of=as_of, branch_scope=_scope(request, entity))
        data = _with_unassigned({
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                # All three, because they answer different questions: what we owe
                # (the AP control), what we have paid ahead of a bill (a separate
                # asset), and the vendor's net position. Only the first is a payable.
                "total_outstanding": _kobo(report.total_outstanding),
                "total_unallocated_credit": _kobo(report.total_unallocated_credit),
                "total_net": _kobo(report.total_net),
        }, report)
        return _paginated_report(
            request, report.rows, data, key="rows",
            render=lambda r: {
                "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                "payment_terms": r.payment_terms,
                "buckets": {b: _kobo(v) for b, v in r.buckets.items()},
                "outstanding": _kobo(r.outstanding),
                "unallocated_credit": _kobo(r.unallocated_credit),
                "net": _kobo(r.net),
            },
            message="AP aging retrieved.",
        )


class APReconciliationView(_ProcBase):
    """Reconcile entity AP subledger balances to the GL control account.

    Deliberately **not** branch-narrowed. Its subject is the AP control account in the
    general ledger, and the GL has no branch column, so there is no branch-level control
    balance for a subledger slice to be compared against. Narrowing only the subledger
    half would report a non-zero ``difference`` on every branch-bound read and bury the
    real "a posting bypassed the subledger" alarm this endpoint exists to raise. It
    carries no document rows, only the two entity totals and their difference.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return subledger, control, and difference in explicit money units."""
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
    """Return the entity's received-not-invoiced clearing balance.

    Deliberately **not** branch-narrowed, for the same reason as the AP reconciliation:
    it reads the GR/IR clearing account straight out of the general ledger, which has no
    branch dimension to slice. The branch-aware view of the same position is the GR/IR
    aging report, which walks the receipts rather than the ledger.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return the entity control balance as explicit kobo/naira data."""
        from ..reports import grir_balance

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        balance = grir_balance(entity, as_of=as_of)
        return success_response(
            "GR/IR clearing balance retrieved.",
            data={
                "entity": entity.code,
                "as_of": str(as_of) if as_of else None,
                "grir_balance": _kobo(balance),
                "is_clear": balance == 0,
            },
        )


class APCashRequirementsView(_ProcBase):
    """Forecast posted unpaid invoice balances by future due window."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return an entity/as-of cash forecast with explicit money objects."""
        from ..reports import FORECAST_BUCKETS, ap_cash_requirements

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = ap_cash_requirements(
            entity, as_of=as_of, branch_scope=_scope(request, entity),
        )
        data = _with_unassigned({
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(FORECAST_BUCKETS),
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_due": _kobo(report.total_due),
                "total_unallocated_credit": _kobo(report.total_unallocated_credit),
                "net_cash_requirement": _kobo(report.net_cash_requirement),
        }, report)
        return _paginated_report(
            request, report.rows, data, key="rows",
            render=lambda r: {
                "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                "buckets": {b: _kobo(v) for b, v in r.buckets.items()},
                "total": _kobo(r.total),
                "unallocated_credit": _kobo(r.unallocated_credit),
                "net_total": _kobo(r.net_total),
            },
            message="AP cash-requirements forecast retrieved.",
        )


class GRIRAgingView(_ProcBase):
    """Age posted receipt value not yet cleared by vendor invoices."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return GRN-grain clearing evidence and GL reconciliation totals."""
        from ..reports import AGING_BUCKETS, grir_aging

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = grir_aging(entity, as_of=as_of, branch_scope=_scope(request, entity))
        data = _with_unassigned({
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_open": _kobo(report.total_open),
                # Null for a branch-narrowed caller: the GL carries no branch, so there is
                # no branch-level control balance to reconcile the receipt walk against.
                # The entity-level control stays on the GR/IR balance endpoint.
                "control_balance": (
                    _kobo(report.control_balance)
                    if report.control_balance is not None else None
                ),
                "difference": (
                    _kobo(report.difference) if report.difference is not None else None
                ),
        }, report)
        return _paginated_report(
            request, report.rows, data, key="rows",
            render=lambda r: {
                "grn_id": r.grn_id, "reference": r.reference,
                "vendor_code": r.vendor_code, "vendor_name": r.vendor_name,
                "received_date": str(r.received_date), "days": r.days,
                "bucket": r.bucket,
                "received_value": _kobo(r.received_value),
                "invoiced_value": _kobo(r.invoiced_value),
                "open_value": _kobo(r.open_value),
            },
            message="GR/IR aging retrieved.",
        )


class APAgingVendorDetailView(_ProcBase):
    """docstring-name: AP aging - vendor detail

    Per-vendor AP drawer: aging buckets + the vendor's open POSTED bills. Report-gated
    so a report viewer can open it without holding ``vendor_invoice.view``.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return one entity vendor's report-scoped open-bill evidence."""
        from ..reports import AGING_BUCKETS, ap_vendor_open_bills

        entity = resolve_entity(request)
        # Entity-scoped vendor resolution - a foreign vendor 404s rather than leaking.
        vendor = _resolve_vendor(entity, request.query_params.get("vendor"))
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = ap_vendor_open_bills(
            entity, vendor, as_of=as_of, branch_scope=_scope(request, entity),
        )
        data = {
                "entity": entity.code, "as_of": str(detail.as_of),
                "buckets": list(AGING_BUCKETS),
                "vendor": {"id": detail.vendor_id, "code": detail.code, "name": detail.name},
                "bucket_amounts": {b: _kobo(v) for b, v in detail.buckets.items()},
                "outstanding": _kobo(detail.outstanding),
                "unallocated_credit": _kobo(detail.unallocated_credit),
                "net": _kobo(detail.net),
        }
        return _paginated_report(
            request, detail.invoices, data, key="invoices",
            render=lambda inv: {
                "invoice_id": inv.invoice_id, "document_number": inv.document_number,
                "invoice_date": str(inv.invoice_date),
                "due_date": str(inv.due_date) if inv.due_date else None,
                "days_overdue": inv.days_overdue, "bucket": inv.bucket,
                "balance_due": _kobo(inv.balance_due),
                "payment_status": inv.payment_status,
            },
            message="Vendor AP detail retrieved.",
        )


class GRIRGrnDetailView(_ProcBase):
    """docstring-name: GR/IR aging - GRN detail

    Per-GRN GR/IR drawer: the reconciliation figures + linked PO and matched invoices.
    Report-gated so a report viewer can open it without holding ``goods_receipt.view``.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return one entity GRN's clearing evidence or an indistinguishable 404."""
        from ..reports import grir_grn_detail

        entity = resolve_entity(request)
        grn_ref = request.query_params.get("grn")
        if not grn_ref or not str(grn_ref).isdigit():
            raise ValidationError({"grn": "A numeric GRN id is required."})
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = grir_grn_detail(
            entity, int(grn_ref), as_of=as_of, branch_scope=_scope(request, entity),
        )
        if detail is None:
            # A receipt in another branch is reported exactly like one that does not
            # exist, so the drawer is not an id-discovery channel.
            raise NotFound("No such goods-received note in this entity.")
        data = {
                "entity": entity.code,
                "grn_id": detail.grn_id, "reference": detail.reference,
                "vendor_code": detail.vendor_code, "vendor_name": detail.vendor_name,
                "received_date": str(detail.received_date),
                "days": detail.days, "bucket": detail.bucket,
                "po_number": detail.po_number or None,
                "received_value": _kobo(detail.received_value),
                "invoiced_value": _kobo(detail.invoiced_value),
                "open_value": _kobo(detail.open_value),
        }
        return _paginated_report(
            request, detail.invoices, data, key="invoices",
            render=lambda vi: {
                "id": vi["id"], "document_number": vi["document_number"],
                "invoice_date": vi["invoice_date"], "net": _kobo(vi["net"]),
            },
            message="GR/IR GRN detail retrieved.",
        )


class GRIRPoLinesView(_ProcBase):
    """docstring-name: GR/IR PO-line report

    Line-grain GR/IR reconciliation: per live PO line, ordered vs received vs invoiced
    (quantity + kobo value) with a derived Cleared / Received>Invoiced / Invoiced>Received
    status. Feeds the prototype's PO-line GR/IR table. Report-gated, entity-scoped.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return line-grain quantity and kobo reconciliation for one entity."""
        from ..reports import grir_po_lines

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = grir_po_lines(entity, as_of=as_of, branch_scope=_scope(request, entity))
        data = {
                "entity": entity.code, "as_of": str(report.as_of),
        }
        return _paginated_report(
            request, report.rows, data, key="rows",
            render=lambda r: {
                "po_line_id": r.po_line_id, "po_line_ref": r.po_line_ref,
                "item": r.item,
                "vendor_code": r.vendor_code, "vendor_name": r.vendor_name,
                "ordered_qty": r.ordered_qty, "received_qty": r.received_qty,
                "invoiced_qty": r.invoiced_qty,
                "received_value": _kobo(r.received_value),
                "invoiced_value": _kobo(r.invoiced_value),
                "grir_balance": _kobo(r.grir_balance),
                "status": r.status,
            },
            message="GR/IR PO-line report retrieved.",
        )


class GRIRPoLineDetailView(_ProcBase):
    """docstring-name: GR/IR PO-line detail

    Per-PO-line GR/IR drawer: the reconciliation figures + the linked POSTED goods
    receipts and vendor invoices. Report-gated so a report viewer can open it without
    holding purchase_order/goods_receipt view keys; a foreign PO-line id 404s.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return one entity PO line's posted receipt and invoice evidence."""
        from ..reports import grir_po_line_detail

        entity = resolve_entity(request)
        line_ref = request.query_params.get("po_line")
        if not line_ref or not str(line_ref).isdigit():
            raise ValidationError({"po_line": "A numeric PO-line id is required."})
        as_of = _date(request.query_params.get("as_of"), "as_of")
        detail = grir_po_line_detail(
            entity, int(line_ref), as_of=as_of, branch_scope=_scope(request, entity),
        )
        if detail is None:
            # A line on another branch's order is reported exactly like a missing one.
            raise NotFound("No such purchase-order line in this entity.")
        data = {
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
        }
        return _paginated_report(
            request, detail.invoices, data, key="invoices",
            render=lambda vi: {
                "id": vi["id"], "document_number": vi["document_number"],
                "invoice_date": vi["invoice_date"],
                "quantity": vi["quantity"], "net": _kobo(vi["net"]),
            },
            message="GR/IR PO-line detail retrieved.",
        )


# --------------------------------------------------------------------------- #
# Procurement analytics                                                        #
# --------------------------------------------------------------------------- #

class ProcurementDashboardView(_ProcBase):
    """Return the permission-aware procurement dashboard for one entity.

    Every document-derived figure is narrowed to the caller's branch, so a
    branch-bound viewer's spend, order pipeline, overdue bills and approval cards
    reconcile with the lists they can actually open.
    """
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Delegate KPI composition while preserving user-dependent visibility."""
        from ..dashboard import procurement_dashboard

        entity = resolve_entity(request)
        return success_response(
            "Procurement dashboard retrieved.",
            data=procurement_dashboard(
                entity, user=request.user, branch_scope=_scope(request, entity),
            ),
        )


class SpendAnalysisView(_ProcBase):
    """Analyze posted invoice spend across isolated date/category filters."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Apply one filter set consistently to vendor, category, period, and totals."""
        from ..reports import spend_analysis

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        _validate_date_window(start, end)
        # Optional ?category=<code|UNCATEGORISED> scopes the whole report to one category.
        category_ref = str(request.query_params.get("category") or "").strip()
        category = None
        if category_ref:
            if category_ref.casefold() == "uncategorised":
                category = "UNCATEGORISED"
            else:
                from ..models import VendorCategory

                category_row = VendorCategory.objects.filter(
                    entity=entity, code__iexact=category_ref,
                ).only("code").first()
                if category_row is None:
                    raise ValidationError({
                        "category": "Unknown vendor category for this entity.",
                    })
                category = category_row.code
        report = spend_analysis(
            entity, start_date=start, end_date=end, category=category,
            branch_scope=_scope(request, entity),
        )

        def _rows(rows):
            """Render one grouped spend dimension with explicit money units."""
            return [
                {
                    "key": r.key, "label": r.label,
                    "net": _kobo(r.net), "tax": _kobo(r.tax), "gross": _kobo(r.gross),
                    "invoice_count": r.invoice_count,
                }
                for r in rows
            ]

        data = _with_unassigned({
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "category": category,
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
        }, report)
        return _paginated_report(
            request, report.by_vendor, data, key="by_vendor",
            render=lambda r: {
                "key": r.key, "label": r.label,
                "net": _kobo(r.net), "tax": _kobo(r.tax), "gross": _kobo(r.gross),
                "invoice_count": r.invoice_count,
            },
            message="Spend analysis retrieved.",
        )


class VendorPerformanceView(_ProcBase):
    """Compare computed fulfilment/payment evidence with recorded assessments."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return entity vendor metrics for one isolated date window."""
        from ..reports import vendor_performance

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        _validate_date_window(start, end)
        report = vendor_performance(
            entity, start_date=start, end_date=end,
            branch_scope=_scope(request, entity),
        )
        data = _with_unassigned({
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
        }, report)

        def render_vendor(r):
            a = r.latest_assessment
            return {
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
                "latest_assessment": (
                    {
                        "quality_acceptance": a.quality_acceptance,
                        "invoice_accuracy": a.invoice_accuracy,
                        "responsiveness": a.responsiveness,
                        "overall_score": a.overall_score,
                        "grade": a.grade,
                        "assessment_date": str(a.assessment_date),
                    } if a else None
                ),
            }

        return _paginated_report(
            request, report.rows, data, key="rows", render=render_vendor,
            message="Vendor performance retrieved.",
        )


class ProcurementCycleTimeView(_ProcBase):
    """Measure evidence-backed elapsed days between procurement lifecycle stages."""
    rbac_permission = "procurement.report.view"

    def get(self, request):
        """Return per-stage and end-to-end samples for one entity/date window."""
        from ..reports import procurement_cycle_time

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        _validate_date_window(start, end)
        report = procurement_cycle_time(
            entity, start_date=start, end_date=end,
            branch_scope=_scope(request, entity),
        )
        return success_response(
            "Procurement cycle time retrieved.",
            data=_with_unassigned({
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "stages": [
                    {
                        "name": s.name, "label": s.label,
                        "sample_count": s.sample_count, "avg_days": s.avg_days,
                        "excluded_count": s.excluded_count,
                    }
                    for s in report.stages
                ],
                "end_to_end_avg_days": report.end_to_end_avg_days,
                "end_to_end_count": report.end_to_end_count,
                "end_to_end_excluded_count": report.end_to_end_excluded_count,
            }, report),
        )
