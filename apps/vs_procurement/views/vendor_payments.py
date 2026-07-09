"""Vendor payments.
"""
from __future__ import annotations  # Keep annotations lazy and avoid runtime import coupling.


from rest_framework.exceptions import NotFound, ValidationError  # API-facing 404 and 400 errors.

from core.response import success_response  # Shared success response envelope.
from vs_finance.views import resolve_entity  # Resolve the current finance entity from the request.

from .. import payables  # Procurement payable posting and allocation service functions.
from ..models import (
    VendorInvoice,  # Vendor bill model used when explicit allocations are posted.
    VendorPayment,  # Vendor payment model managed by these endpoints.
)
from ..serializers import (
    VendorPaymentSerializer,  # Response serializer for vendor payment payloads.
)


from .base import (
    _ProcBase,  # Procurement API base class with RBAC and pagination helpers.
    _date,  # Parse request dates consistently.
    _money,  # Parse integer kobo request amounts consistently.
    _resolve_account,  # Resolve account identifiers into account records.
    _resolve_currency,  # Resolve currency codes for the entity.
    _resolve_tax,  # Resolve tax code identifiers for withholding tax.
    _resolve_vendor,  # Resolve vendor identifiers for the entity.
)

# --------------------------------------------------------------------------- #
# Vendor payments                                                             #
# --------------------------------------------------------------------------- #

class VendorPaymentListCreateView(_ProcBase):  # List vendor payments and create draft payments.
    """GET (list) / POST (create a draft payment ready to post).

    docstring-name: Vendor payments
    """

    @property
    def rbac_permission(self):  # Choose permission dynamically by HTTP method.
        return "procurement.vendor_payment.create" if self.request.method == "POST" \
            else "procurement.vendor_payment.view"  # Read permission is enough for GET.

    def get(self, request):  # Handle GET /vendor-payments.
        entity = resolve_entity(request)  # Scope all results to the active entity.
        qs = VendorPayment.objects.filter(entity=entity).select_related("vendor").prefetch_related("allocations")  # Base queryset with related data loaded.
        if (status_ := request.query_params.get("status")):  # Optional status filter from the query string.
            qs = qs.filter(status=status_)  # Narrow results to the requested lifecycle status.
        return self.paginate(request, qs.order_by("-id"), VendorPaymentSerializer)  # Return newest payments first.

    def post(self, request):  # Handle POST /vendor-payments.
        entity = resolve_entity(request)  # Scope the new payment to the active entity.
        body = request.data  # Keep request data local for repeated reads.
        vendor = _resolve_vendor(entity, body.get("vendor"))  # Resolve the payment vendor.
        gross = _money(body.get("gross_amount", 0), "gross_amount")  # Parse gross payment amount in kobo.
        if gross <= 0:  # Payments must move a positive amount.
            raise ValidationError({"gross_amount": "A positive gross amount (kobo) is required."})
        wht = _money(body.get("wht_amount", 0), "wht_amount")  # Parse withholding tax amount in kobo.
        payment_account = _resolve_account(entity, body.get("payment_account"), "payment_account")  # Resolve bank/cash account.
        if payment_account is None:  # A payment cannot post without a cash/bank credit account.
            raise ValidationError({"payment_account": "A bank/cash payment account is required."})
        payment = VendorPayment.objects.create(  # Create the draft payment document.
            entity=entity, vendor=vendor,  # Persist ownership and vendor relationship.
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),  # Require a payment date.
            currency=_resolve_currency(entity, body.get("currency")),  # Use requested or default currency.
            method=body.get("method") or "BANK_TRANSFER",  # Default missing method to bank transfer.
            gross_amount=gross, wht_amount=wht, net_amount=gross - wht,  # Store gross, WHT, and net cash paid.
            payment_account=payment_account,  # Store the cash/bank account used at posting.
            wht_tax_code=_resolve_tax(entity, body.get("wht_tax_code"), "wht_tax_code"),  # Optional withholding tax code.
            reference=body.get("reference", ""), narration=body.get("narration", ""),  # Preserve optional audit fields.
            created_by=request.user if request.user.is_authenticated else None,  # Attribute creation when authenticated.
        )
        return success_response(  # Return the created draft payment.
            "Vendor payment created.", data=VendorPaymentSerializer(payment).data, status=201,  # Serialize with HTTP 201.
        )


class VendorPaymentDetailView(_ProcBase):  # Retrieve one vendor payment.
    """docstring-name: Vendor payments"""
    rbac_permission = "procurement.vendor_payment.view"  # Detail reads require payment view permission.

    def get(self, request, pk):  # Handle GET /vendor-payments/{pk}.
        entity = resolve_entity(request)  # Scope lookup to the active entity.
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()  # Fetch the payment or None.
        if payment is None:  # Hide payments outside the entity and missing rows the same way.
            raise NotFound("No such vendor payment in this entity.")
        return success_response("Vendor payment retrieved.", data=VendorPaymentSerializer(payment).data)  # Return serialized payment.


class VendorPaymentPostView(_ProcBase):  # Post and optionally allocate a vendor payment.
    """POST — post the payment (Dr AP gross, Cr bank net, Cr WHT) and allocate it.

    Body (optional): ``auto_allocate`` (default true) settles oldest bills first;
    ``allocations`` = ``[{"vendor_invoice": <id>, "amount": <kobo>}, ...]`` for an
    explicit split.

    docstring-name: Post a vendor payment
    """

    rbac_permission = "procurement.vendor_payment.post"  # Posting requires the stronger payment permission.

    def post(self, request, pk):  # Handle POST /vendor-payments/{pk}/post.
        entity = resolve_entity(request)  # Scope lookup and allocations to the active entity.
        payment = VendorPayment.objects.filter(entity=entity, pk=pk).first()  # Fetch the draft payment.
        if payment is None:  # Reject missing or cross-entity payment IDs.
            raise NotFound("No such vendor payment in this entity.")

        allocations = None  # None means the service may auto-allocate.
        if request.data.get("allocations"):  # Explicit allocations override auto oldest-first allocation.
            allocations = []  # Build the service allocation tuples.
            for item in request.data["allocations"]:  # Validate each requested bill allocation.
                inv = VendorInvoice.objects.filter(entity=entity, pk=item.get("vendor_invoice")).first()  # Resolve bill in entity.
                if inv is None:  # Explicit allocations cannot reference missing bills.
                    raise ValidationError(
                        {"allocations": f"No such vendor invoice {item.get('vendor_invoice')}."})
                allocations.append((inv, _money(item.get("amount", 0), "amount")))  # Store bill and kobo amount.

        payables.post_vendor_payment(  # Delegate posting, journal creation, WHT, and allocation.
            payment, actor_user=request.user,  # Attribute posting to the current user.
            auto_allocate=bool(request.data.get("auto_allocate", True)),  # Default to auto allocation.
            allocations=allocations,  # Pass explicit allocation tuples when supplied.
        )
        payment.refresh_from_db()  # Reload status, journal, and allocation totals after service updates.
        return success_response(  # Return the posted payment.
            f"Vendor payment {payment.document_number} posted.",  # Include the document number in the response message.
            data=VendorPaymentSerializer(payment).data,  # Serialize the final persisted state.
        )

