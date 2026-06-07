"""REST API for vs_procurement — the Procure-to-Pay surface at ``/v1/procurement/``.

Entity-scoped (``?entity=<id|code>``), platform-envelope, RBAC-gated
(``procurement.<resource>.<action>``) endpoints over the purchasing chain and the AP
sub-ledger:

    requisitions → purchase-orders → goods-receipts → vendor-invoices → vendor-payments

The views stay thin: they parse the request, resolve GL accounts / tax codes / vendors
by **code or id**, build the documents and hand off to the purchasing/payables
**services** (which own every journal posting, the three-way match, GR/IR clearing and
WHT). Domain errors raised by the services render through the shared typed-exception
handler, so success paths read cleanly here.

Money is integer **kobo** throughout; never a float.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from . import payables, purchasing
from .models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    Vendor,
    VendorCategory,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
)
from .serializers import (
    GoodsReceivedNoteSerializer,
    PurchaseOrderSerializer,
    RequisitionSerializer,
    VendorCategorySerializer,
    VendorInvoiceSerializer,
    VendorPaymentSerializer,
    VendorSerializer,
)


# --------------------------------------------------------------------------- #
# Shared resolution helpers                                                   #
# --------------------------------------------------------------------------- #

def _resolve_account(entity, ref, field):
    """Resolve a GL account by **code** (e.g. "2100") or id within ``entity``.

    Codes in the Chart of Accounts are numeric strings, so we match on code *first*
    and only fall back to a primary-key lookup — otherwise "2100" would be mistaken
    for a row id. Returns ``None`` when ``ref`` is blank.
    """
    if ref in (None, ""):
        return None
    from vs_finance.models import Account

    qs = Account.objects.filter(entity=entity)
    acc = qs.filter(code=str(ref)).first()
    if acc is None and str(ref).isdigit():
        acc = qs.filter(pk=int(ref)).first()
    if acc is None:
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    return acc


def _resolve_tax(entity, ref, field="tax_code"):
    if ref in (None, ""):
        return None
    from vs_finance.models import TaxCode

    qs = TaxCode.objects.filter(entity=entity)
    tc = qs.filter(code=str(ref)).first()
    if tc is None and str(ref).isdigit():
        tc = qs.filter(pk=int(ref)).first()
    if tc is None:
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    return tc


def _resolve_currency(entity, ref, field="currency"):
    if ref in (None, ""):
        return None
    from vs_finance.models import Currency

    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur


def _resolve_vendor(entity, ref):
    if ref in (None, ""):
        raise ValidationError({"vendor": "A vendor is required."})
    qs = Vendor.objects.filter(entity=entity)
    vendor = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref)).first() or qs.filter(code=str(ref).upper()).first()
    )
    if vendor is None:
        raise ValidationError({"vendor": f"No vendor '{ref}' in this entity."})
    return vendor


def _date(value, field, *, required=False):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


def _dec(value, field):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})


def _money(value, field):
    """Coerce to non-negative integer kobo, rejecting floats-as-naira mistakes."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo."})
    if amount < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValidationError({"lines": "At least one line is required."})
    return lines


class _ProcBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]


# --------------------------------------------------------------------------- #
# Vendor categories + vendors                                                 #
# --------------------------------------------------------------------------- #

class VendorCategoryListCreateView(_ProcBase):
    """GET (list) / POST (create) vendor categories for an entity."""

    @property
    def rbac_permission(self):
        return "procurement.category.create" if self.request.method == "POST" \
            else "procurement.category.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorCategory.objects.filter(entity=entity).select_related("default_expense_account")
        return success_response(
            "Vendor categories retrieved.",
            data=VendorCategorySerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        cat = VendorCategory.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Vendor category created.", data=VendorCategorySerializer(cat).data, status=201,
        )


class VendorListCreateView(_ProcBase):
    """GET (list) / POST (create) vendors for an entity."""

    @property
    def rbac_permission(self):
        return "procurement.vendor.create" if self.request.method == "POST" \
            else "procurement.vendor.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Vendor.objects.filter(entity=entity).select_related("category", "payable_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (hold := request.query_params.get("on_hold")) in ("true", "false"):
            qs = qs.filter(on_hold=hold == "true")
        return success_response(
            "Vendors retrieved.", data=VendorSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        category = None
        if body.get("category"):
            category = VendorCategory.objects.filter(entity=entity, pk=body["category"]).first() \
                or VendorCategory.objects.filter(entity=entity, code=body["category"]).first()
            if category is None:
                raise ValidationError({"category": "No such vendor category in this entity."})
        vendor = Vendor.objects.create(
            entity=entity, code=body["code"], name=body["name"], category=category,
            email=body.get("email", ""), phone=body.get("phone", ""),
            tax_id=body.get("tax_id", ""),
            bank_name=body.get("bank_name", ""),
            bank_account_number=body.get("bank_account_number", ""),
            bank_account_name=body.get("bank_account_name", ""),
            payable_account=_resolve_account(entity, body.get("payable_account"), "payable_account"),
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            default_wht_tax_code=_resolve_tax(entity, body.get("default_wht_tax_code"),
                                              "default_wht_tax_code"),
            payment_terms=body.get("payment_terms") or "NET_30",
            kyc_status=body.get("kyc_status") or "PENDING",
            risk=body.get("risk") or "LOW",
            on_hold=bool(body.get("on_hold", False)),
        )
        return success_response(
            "Vendor created.", data=VendorSerializer(vendor).data, status=201,
        )


class VendorDetailView(_ProcBase):
    rbac_permission = "procurement.vendor.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        vendor = Vendor.objects.filter(entity=entity, pk=pk).first()
        if vendor is None:
            raise NotFound("No such vendor in this entity.")
        return success_response("Vendor retrieved.", data=VendorSerializer(vendor).data)


# --------------------------------------------------------------------------- #
# Purchase requisitions                                                       #
# --------------------------------------------------------------------------- #

class RequisitionListCreateView(_ProcBase):
    """GET (list) / POST (create draft + lines) purchase requisitions."""

    @property
    def rbac_permission(self):
        return "procurement.requisition.create" if self.request.method == "POST" \
            else "procurement.requisition.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PurchaseRequisition.objects.filter(entity=entity).prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return success_response(
            "Requisitions retrieved.",
            data=RequisitionSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        req = PurchaseRequisition.objects.create(
            entity=entity,
            request_date=_date(body.get("request_date"), "request_date", required=True),
            needed_by=_date(body.get("needed_by"), "needed_by"),
            justification=body.get("justification", ""),
            requested_by=request.user if request.user.is_authenticated else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            PurchaseRequisitionLine.objects.create(
                requisition=req, line_no=ln.get("line_no", i),
                description=ln.get("description", ""),
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                estimated_unit_price=_money(ln.get("estimated_unit_price", 0), "estimated_unit_price"),
                expense_account=_resolve_account(entity, ln.get("expense_account"), "expense_account"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        req.recompute_total(save=True)
        return success_response(
            "Requisition created.", data=RequisitionSerializer(req).data, status=201,
        )


class RequisitionDetailView(_ProcBase):
    rbac_permission = "procurement.requisition.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        return success_response("Requisition retrieved.", data=RequisitionSerializer(req).data)


class RequisitionSubmitView(_ProcBase):
    rbac_permission = "procurement.requisition.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        purchasing.submit_requisition(req, actor_user=request.user)
        return success_response("Requisition submitted.", data=RequisitionSerializer(req).data)


class RequisitionApproveView(_ProcBase):
    rbac_permission = "procurement.requisition.approve"

    def post(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        purchasing.approve_requisition(req, actor_user=request.user)
        return success_response("Requisition approved.", data=RequisitionSerializer(req).data)


# --------------------------------------------------------------------------- #
# Purchase orders                                                             #
# --------------------------------------------------------------------------- #

class PurchaseOrderListCreateView(_ProcBase):
    """GET (list) / POST (create from an approved requisition)."""

    @property
    def rbac_permission(self):
        return "procurement.purchase_order.create" if self.request.method == "POST" \
            else "procurement.purchase_order.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PurchaseOrder.objects.filter(entity=entity).select_related("vendor").prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        return success_response(
            "Purchase orders retrieved.",
            data=PurchaseOrderSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        req = PurchaseRequisition.objects.filter(entity=entity, pk=body.get("requisition")).first()
        if req is None:
            raise ValidationError({"requisition": "An approved requisition is required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        po = purchasing.create_po_from_requisition(
            req, vendor=vendor,
            order_date=_date(body.get("order_date"), "order_date", required=True),
            expected_date=_date(body.get("expected_date"), "expected_date"),
            currency=_resolve_currency(entity, body.get("currency")),
            actor_user=request.user,
        )
        return success_response(
            "Purchase order created.", data=PurchaseOrderSerializer(po).data, status=201,
        )


class PurchaseOrderDetailView(_ProcBase):
    rbac_permission = "procurement.purchase_order.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        po = PurchaseOrder.objects.filter(entity=entity, pk=pk).first()
        if po is None:
            raise NotFound("No such purchase order in this entity.")
        return success_response("Purchase order retrieved.", data=PurchaseOrderSerializer(po).data)


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
                from .models import PurchaseOrderLine
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
                from .models import PurchaseOrderLine
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


# --------------------------------------------------------------------------- #
# Vendor payments                                                             #
# --------------------------------------------------------------------------- #

class VendorPaymentListCreateView(_ProcBase):
    """GET (list) / POST (create a draft payment ready to post)."""

    @property
    def rbac_permission(self):
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorPayment.objects.filter(entity=entity).select_related("vendor").prefetch_related("allocations")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return success_response(
            "Vendor payments retrieved.",
            data=VendorPaymentSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        vendor = _resolve_vendor(entity, body.get("vendor"))
        gross = _money(body.get("gross_amount", 0), "gross_amount")
        if gross <= 0:
            raise ValidationError({"gross_amount": "A positive gross amount (kobo) is required."})
        wht = _money(body.get("wht_amount", 0), "wht_amount")
        payment_account = _resolve_account(entity, body.get("payment_account"), "payment_account")
        if payment_account is None:
            raise ValidationError({"payment_account": "A bank/cash payment account is required."})
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor,
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            currency=_resolve_currency(entity, body.get("currency")),
            method=body.get("method") or "BANK_TRANSFER",
            gross_amount=gross, wht_amount=wht, net_amount=gross - wht,
            payment_account=payment_account,
            wht_tax_code=_resolve_tax(entity, body.get("wht_tax_code"), "wht_tax_code"),
            reference=body.get("reference", ""), narration=body.get("narration", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        return success_response(
            "Vendor payment created.", data=VendorPaymentSerializer(payment).data, status=201,
        )


class VendorPaymentDetailView(_ProcBase):
    rbac_permission = "procurement.vendor_payment.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")
        return success_response("Vendor payment retrieved.", data=VendorPaymentSerializer(payment).data)


class VendorPaymentPostView(_ProcBase):
    """POST — post the payment (Dr AP gross, Cr bank net, Cr WHT) and allocate it.

    Body (optional): ``auto_allocate`` (default true) settles oldest bills first;
    ``allocations`` = ``[{"vendor_invoice": <id>, "amount": <kobo>}, ...]`` for an
    explicit split.
    """

    rbac_permission = "procurement.vendor_payment.post"

    def post(self, request, pk):
        entity = resolve_entity(request)
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()
        if payment is None:
            raise NotFound("No such vendor payment in this entity.")

        allocations = None
        if request.data.get("allocations"):
            allocations = []
            for item in request.data["allocations"]:
                inv = VendorInvoice.objects.filter(entity=entity, pk=item.get("vendor_invoice")).first()
                if inv is None:
                    raise ValidationError(
                        {"allocations": f"No such vendor invoice {item.get('vendor_invoice')}."})
                allocations.append((inv, _money(item.get("amount", 0), "amount")))

        payables.post_vendor_payment(
            payment, actor_user=request.user,
            auto_allocate=bool(request.data.get("auto_allocate", True)),
            allocations=allocations,
        )
        payment.refresh_from_db()
        return success_response(
            f"Vendor payment {payment.document_number} posted.",
            data=VendorPaymentSerializer(payment).data,
        )


# --------------------------------------------------------------------------- #
# AP reports                                                                  #
# --------------------------------------------------------------------------- #

def _kobo(amount):
    return {"kobo": amount, "naira": format_naira(amount)}


class APAgingView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import AGING_BUCKETS, ap_aging

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
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import reconcile_ap

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
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import grir_balance

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
