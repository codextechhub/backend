"""Vendor payments.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import payables
from ..models import (
    VendorInvoice,
    VendorPayment,
)
from ..serializers import (
    VendorPaymentSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _money,
    _resolve_account,
    _resolve_currency,
    _resolve_tax,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# Vendor payments                                                             #
# --------------------------------------------------------------------------- #

class VendorPaymentListCreateView(_ProcBase):
    """GET (list) / POST (create a draft payment ready to post).

    docstring-name: Vendor payments
    """

    @property
    def rbac_permission(self):
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorPayment.objects.filter(entity=entity).select_related("vendor").prefetch_related("allocations")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-id"), VendorPaymentSerializer)

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
    """docstring-name: Vendor payments"""
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

    docstring-name: Post a vendor payment
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


