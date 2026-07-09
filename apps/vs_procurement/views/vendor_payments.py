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

# List vendor payments and create draft payments.
class VendorPaymentListCreateView(_ProcBase):
    """GET (list) / POST (create a draft payment ready to post).

    docstring-name: Vendor payments
    """

    @property
    # Choose permission dynamically by HTTP method.
    def rbac_permission(self):
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"  # Read permission is enough for GET.

    # Handle GET /vendor-payments.
    def get(self, request):
        entity = resolve_entity(request)  # Scope all results to the active entity.
        qs = VendorPayment.objects.filter(entity=entity).select_related("vendor").prefetch_related("allocations")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-id"), VendorPaymentSerializer)

    # Handle POST /vendor-payments.
    def post(self, request):
        entity = resolve_entity(request)  # Scope the new payment to the active entity.
        body = request.data  # Keep request data local for repeated reads.
        vendor = _resolve_vendor(entity, body.get("vendor"))
        gross = _money(body.get("gross_amount", 0), "gross_amount")
        if gross <= 0:  # Payments must move a positive amount.
            raise ValidationError({"gross_amount": "A positive gross amount (kobo) is required."})
        wht = _money(body.get("wht_amount", 0), "wht_amount")
        payment_account = _resolve_account(entity, body.get("payment_account"), "payment_account")
        if payment_account is None:  # A payment cannot post without a cash/bank credit account.
            raise ValidationError({"payment_account": "A bank/cash payment account is required."})
        payment = VendorPayment.objects.create(
            entity=entity, vendor=vendor,  # Persist ownership and vendor relationship.
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            currency=_resolve_currency(entity, body.get("currency")),
            method=body.get("method") or "BANK_TRANSFER",
            gross_amount=gross, wht_amount=wht, net_amount=gross - wht,  # Store gross, WHT, and net cash paid.
            payment_account=payment_account,  # Store the cash/bank account used at posting.
            wht_tax_code=_resolve_tax(entity, body.get("wht_tax_code"), "wht_tax_code"),
            reference=body.get("reference", ""), narration=body.get("narration", ""),
            created_by=request.user if request.user.is_authenticated else None,  # Attribute creation when authenticated.
        )
        return success_response(
            "Vendor payment created.", data=VendorPaymentSerializer(payment).data, status=201,  # Serialize with HTTP 201.
        )


# Retrieve one vendor payment.
class VendorPaymentDetailView(_ProcBase):
    """docstring-name: Vendor payments"""
    rbac_permission = "procurement.vendor_payment.view"  # Detail reads require payment view permission.

    # Handle GET /vendor-payments/{pk}.
    def get(self, request, pk):
        entity = resolve_entity(request)  # Scope lookup to the active entity.
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()
        if payment is None:  # Hide payments outside the entity and missing rows the same way.
            raise NotFound("No such vendor payment in this entity.")
        return success_response("Vendor payment retrieved.", data=VendorPaymentSerializer(payment).data)


# Post and optionally allocate a vendor payment.
class VendorPaymentPostView(_ProcBase):
    """POST — post the payment (Dr AP gross, Cr bank net, Cr WHT) and allocate it.

    Body (optional): ``auto_allocate`` (default true) settles oldest bills first;
    ``allocations`` = ``[{"vendor_invoice": <id>, "amount": <kobo>}, ...]`` for an
    explicit split.

    docstring-name: Post a vendor payment
    """

    rbac_permission = "procurement.vendor_payment.post"  # Posting requires the stronger payment permission.

    # Handle POST /vendor-payments/{pk}/post.
    def post(self, request, pk):
        entity = resolve_entity(request)  # Scope lookup and allocations to the active entity.
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()
        if payment is None:  # Reject missing or cross-entity payment IDs.
            raise NotFound("No such vendor payment in this entity.")

        allocations = None  # None means the service may auto-allocate.
        if request.data.get("allocations"):
            allocations = []  # Build the service allocation tuples.
            for item in request.data["allocations"]:  # Validate each requested bill allocation.
                inv = VendorInvoice.objects.filter(entity=entity, pk=item.get("vendor_invoice")).first()
                if inv is None:  # Explicit allocations cannot reference missing bills.
                    raise ValidationError(
                        {"allocations": f"No such vendor invoice {item.get('vendor_invoice')}."})
                allocations.append((inv, _money(item.get("amount", 0), "amount")))

        payables.post_vendor_payment(  # Delegate posting, journal creation, WHT, and allocation.
            payment, actor_user=request.user,  # Attribute posting to the current user.
            auto_allocate=bool(request.data.get("auto_allocate", True)),
            allocations=allocations,  # Pass explicit allocation tuples when supplied.
        )
        payment.refresh_from_db()
        return success_response(
            f"Vendor payment {payment.document_number} posted.",  # Include the document number in the response message.
            data=VendorPaymentSerializer(payment).data,  # Serialize the final persisted state.
        )

