"""Goods receipts and vendor invoices (3-way match).
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import payables, purchasing
from ..models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    VendorInvoice,
    VendorInvoiceLine,
)
from ..serializers import (
    GoodsReceivedNoteSerializer,
    VendorInvoiceSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _dec,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_currency,
    _resolve_tax,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# Goods received notes                                                        #
# --------------------------------------------------------------------------- #

class GoodsReceiptListCreateView(_ProcBase):
    """GET (list) / POST (create draft GRN + lines)."""

    @property
    def rbac_permission(self):
        return "procurement.goods_receipt.create" if self.request.method == "POST" \
            else "procurement.goods_receipt.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = GoodsReceivedNote.objects.filter(entity=entity).select_related("vendor").prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return success_response(
            "Goods receipts retrieved.",
            data=GoodsReceivedNoteSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
        grn = GoodsReceivedNote.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            received_date=_date(body.get("received_date"), "received_date", required=True),
            reference=body.get("reference", ""), narration=body.get("narration", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            po_line = None
            if ln.get("po_line"):
                from ..models import PurchaseOrderLine
                po_line = PurchaseOrderLine.objects.filter(
                    purchase_order__entity=entity, pk=ln["po_line"]).first()
                if po_line is None:
                    raise ValidationError({"po_line": f"No such PO line {ln['po_line']}."})
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or (po_line.expense_account if po_line else None)
            if expense is None:
                raise ValidationError({"expense_account": "A line expense account is required."})
            GoodsReceivedNoteLine.objects.create(
                grn=grn, po_line=po_line, line_no=ln.get("line_no", i),
                description=ln.get("description", ""),
                expense_account=expense,
                accepted_qty=_dec(ln.get("accepted_qty", 0), "accepted_qty"),
                rejected_qty=_dec(ln.get("rejected_qty", 0), "rejected_qty"),
                unit_price=_money(ln.get("unit_price", po_line.unit_price if po_line else 0), "unit_price"),
            )
        grn.recompute_total(save=True)
        return success_response(
            "Goods receipt created.", data=GoodsReceivedNoteSerializer(grn).data, status=201,
        )


class GoodsReceiptDetailView(_ProcBase):
    rbac_permission = "procurement.goods_receipt.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        grn = GoodsReceivedNote.objects.filter(entity=entity, pk=pk).first()
        if grn is None:
            raise NotFound("No such goods receipt in this entity.")
        return success_response("Goods receipt retrieved.", data=GoodsReceivedNoteSerializer(grn).data)


class GoodsReceiptPostView(_ProcBase):
    """POST — post the GRN (Dr expense, Cr GR/IR clearing)."""

    rbac_permission = "procurement.goods_receipt.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        grn = GoodsReceivedNote.objects.filter(entity=entity, pk=pk).first()
        if grn is None:
            raise NotFound("No such goods receipt in this entity.")
        purchasing.post_grn(grn, actor_user=request.user)
        grn.refresh_from_db()
        return success_response(
            f"Goods receipt {grn.document_number} posted.",
            data=GoodsReceivedNoteSerializer(grn).data,
        )


# --------------------------------------------------------------------------- #
# Vendor invoices (bills)                                                     #
# --------------------------------------------------------------------------- #

class VendorInvoiceListCreateView(_ProcBase):
    """GET (list) / POST (create draft bill + lines)."""

    @property
    def rbac_permission(self):
        return "procurement.vendor_invoice.create" if self.request.method == "POST" \
            else "procurement.vendor_invoice.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorInvoice.objects.filter(entity=entity).select_related("vendor").prefetch_related("lines")
        for param in ("status", "payment_status", "match_status"):
            if (val := request.query_params.get(param)):
                qs = qs.filter(**{param: val})
        return success_response(
            "Vendor invoices retrieved.",
            data=VendorInvoiceSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = None
        if body.get("purchase_order"):
            po = PurchaseOrder.objects.filter(entity=entity, pk=body["purchase_order"]).first()
            if po is None:
                raise ValidationError({"purchase_order": "No such purchase order in this entity."})
        invoice = VendorInvoice.objects.create(
            entity=entity, vendor=vendor, purchase_order=po,
            invoice_date=_date(body.get("invoice_date"), "invoice_date", required=True),
            due_date=_date(body.get("due_date"), "due_date"),
            currency=_resolve_currency(entity, body.get("currency")),
            vendor_reference=body.get("vendor_reference", ""),
            narration=body.get("narration", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            po_line = grn_line = None
            if ln.get("po_line"):
                from ..models import PurchaseOrderLine
                po_line = PurchaseOrderLine.objects.filter(
                    purchase_order__entity=entity, pk=ln["po_line"]).first()
                if po_line is None:
                    raise ValidationError({"po_line": f"No such PO line {ln['po_line']}."})
            if ln.get("grn_line"):
                grn_line = GoodsReceivedNoteLine.objects.filter(
                    grn__entity=entity, pk=ln["grn_line"]).first()
                if grn_line is None:
                    raise ValidationError({"grn_line": f"No such GRN line {ln['grn_line']}."})
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or (po_line.expense_account if po_line else None)
            if expense is None:
                raise ValidationError({"expense_account": "A line expense account is required."})
            VendorInvoiceLine.objects.create(
                vendor_invoice=invoice, po_line=po_line, grn_line=grn_line,
                line_no=ln.get("line_no", i), description=ln.get("description", ""),
                expense_account=expense,
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                unit_price=_money(ln.get("unit_price", 0), "unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        payables.price_vendor_invoice(invoice)
        return success_response(
            "Vendor invoice created.", data=VendorInvoiceSerializer(invoice).data, status=201,
        )


class VendorInvoiceDetailView(_ProcBase):
    rbac_permission = "procurement.vendor_invoice.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        return success_response("Vendor invoice retrieved.", data=VendorInvoiceSerializer(invoice).data)


class VendorInvoiceMatchView(_ProcBase):
    """POST — run the three-way match (PO ↔ GRN ↔ bill) and return the status."""

    rbac_permission = "procurement.vendor_invoice.match"

    def post(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        payables.price_vendor_invoice(invoice)
        payables.match_vendor_invoice(invoice, save=True)
        invoice.refresh_from_db()
        return success_response(
            f"Three-way match: {invoice.match_status}.",
            data=VendorInvoiceSerializer(invoice).data,
        )


class VendorInvoicePostView(_ProcBase):
    """POST — post the bill (Dr GR/IR + input VAT, Cr AP). ``allow_variance`` overrides a flag."""

    rbac_permission = "procurement.vendor_invoice.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        invoice = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("No such vendor invoice in this entity.")
        payables.post_vendor_invoice(
            invoice, actor_user=request.user,
            allow_variance=bool(request.data.get("allow_variance", False)),
        )
        invoice.refresh_from_db()
        return success_response(
            f"Vendor invoice {invoice.document_number} posted.",
            data=VendorInvoiceSerializer(invoice).data,
        )


