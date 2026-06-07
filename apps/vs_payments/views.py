"""REST API for vs_payments.

Two kinds of endpoint:

* **Authenticated, entity-scoped** actions/reads (``?entity=<id|code>``) for initiating
  collections/payouts, provisioning virtual accounts and listing gateway records. These
  use the platform envelope + RBAC (``payments.<resource>.<action>``), exactly like
  ``vs_finance``.
* A **public webhook receiver** (``/webhooks/<provider>/``) that takes the raw signed
  body from the PSP. It is ``AllowAny`` because the PSP can't carry a JWT — authenticity
  comes from the body signature, verified inside :func:`vs_payments.webhooks.ingest_webhook`.

Domain errors raised by the services/webhooks render through the shared typed-exception
handler, so the views stay thin.
"""
from __future__ import annotations

from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.models import Account, Customer, Invoice
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from . import services, webhooks
from .exceptions import DuplicateWebhookError
from .models import CollectionIntent, PayoutInstruction, VirtualAccount
from .serializers import (
    CollectionIntentSerializer,
    PayoutInstructionSerializer,
    VirtualAccountSerializer,
)


def _entity_obj(entity, model, pk, field):
    """Fetch ``model`` by pk within ``entity`` or raise a 400 ValidationError."""
    if pk in (None, ""):
        return None
    obj = model.objects.filter(entity=entity, pk=pk).first()
    if obj is None:
        raise ValidationError({field: f"No {model.__name__.lower()} {pk} in this entity."})
    return obj


# --------------------------------------------------------------------------- #
# Collections                                                                 #
# --------------------------------------------------------------------------- #

class CollectionListCreateView(APIView):
    """GET (list) / POST (initiate) collections for an entity."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        # POST (initiate) needs the stronger 'create'; GET (list) needs only 'view'.
        return "payments.collection.create" if self.request.method == "POST" \
            else "payments.collection.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = CollectionIntent.objects.filter(entity=entity).select_related(
            "customer", "payment",
        )
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        data = CollectionIntentSerializer(qs[:200], many=True).data
        return success_response("Collections retrieved.", data=data)

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        amount = int(body.get("amount") or 0)
        if amount <= 0:
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        customer = _entity_obj(entity, Customer, body.get("customer"), "customer")
        invoice = _entity_obj(entity, Invoice, body.get("invoice"), "invoice")
        deposit = _entity_obj(entity, Account, body.get("deposit_account"), "deposit_account")
        intent = services.initiate_collection(
            entity=entity, amount=amount, customer=customer, invoice=invoice,
            deposit_account=deposit, channel=body.get("channel"),
            provider=body.get("provider"), payer_email=body.get("payer_email", ""),
            payer_name=body.get("payer_name", ""), narration=body.get("narration", ""),
            metadata=body.get("metadata") or {}, actor_user=request.user,
        )
        return success_response(
            "Collection initiated.", data=CollectionIntentSerializer(intent).data, status=201,
        )


class CollectionDetailView(APIView):
    """GET a collection; ``?verify=1`` polls the provider and confirms if settled."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.collection.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        intent = CollectionIntent.objects.filter(entity=entity, pk=pk).first()
        if intent is None:
            raise NotFound("No such collection in this entity.")
        if request.query_params.get("verify") in ("1", "true", "True"):
            intent = services.confirm_collection(intent, actor_user=request.user)
        return success_response("Collection retrieved.", data=CollectionIntentSerializer(intent).data)


class VirtualAccountCreateView(APIView):
    """POST to provision a dedicated virtual account for a customer."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.virtual_account.create"

    def post(self, request):
        entity = resolve_entity(request)
        customer = _entity_obj(entity, Customer, request.data.get("customer"), "customer")
        if customer is None:
            raise ValidationError({"customer": "A customer is required."})
        deposit = _entity_obj(entity, Account, request.data.get("deposit_account"), "deposit_account")
        va = services.create_virtual_account(
            entity=entity, customer=customer, provider=request.data.get("provider"),
            deposit_account=deposit, bank_code=request.data.get("bank_code", ""),
            actor_user=request.user,
        )
        return success_response(
            "Virtual account created.", data=VirtualAccountSerializer(va).data, status=201,
        )


# --------------------------------------------------------------------------- #
# Payouts                                                                     #
# --------------------------------------------------------------------------- #

class PayoutListCreateView(APIView):
    """GET (list) / POST (initiate) payouts for an entity."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.payout.create" if self.request.method == "POST" \
            else "payments.payout.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PayoutInstruction.objects.filter(entity=entity)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return success_response(
            "Payouts retrieved.", data=PayoutInstructionSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        amount = int(body.get("amount") or 0)
        if amount <= 0:
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        for field in ("beneficiary_name", "beneficiary_account_number"):
            if not body.get(field):
                raise ValidationError({field: "This field is required."})
        vendor = None
        if body.get("vendor"):
            from vs_procurement.models import Vendor
            vendor = Vendor.objects.filter(entity=entity, pk=body.get("vendor")).first()
            if vendor is None:
                raise ValidationError({"vendor": "No such vendor in this entity."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")
        payout = services.initiate_payout(
            entity=entity, amount=amount, beneficiary_name=body["beneficiary_name"],
            beneficiary_account_number=body["beneficiary_account_number"],
            beneficiary_bank_code=body.get("beneficiary_bank_code", ""), vendor=vendor,
            source_account=source, provider=body.get("provider"),
            narration=body.get("narration", ""), wht_amount=int(body.get("wht_amount") or 0),
            metadata=body.get("metadata") or {}, actor_user=request.user,
        )
        return success_response(
            "Payout initiated.", data=PayoutInstructionSerializer(payout).data, status=201,
        )


# --------------------------------------------------------------------------- #
# Webhook receiver (public, signature-verified)                               #
# --------------------------------------------------------------------------- #

class WebhookView(APIView):
    """POST /webhooks/<provider>/ — raw signed PSP event. No JWT; signature is the auth."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request, provider):
        try:
            event = webhooks.ingest_webhook(
                provider=provider, raw_body=request.body, headers=dict(request.headers),
            )
        except DuplicateWebhookError:
            # Already handled — acknowledge so the provider stops retrying.
            return success_response("Duplicate event ignored.", data={"duplicate": True})
        return success_response(
            "Webhook processed.", data={"id": event.id, "status": event.status},
        )
