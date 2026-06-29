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

import math

from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.models import Account, Customer, Invoice
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from . import reconciliation, services, webhooks
from .constants import VirtualAccountStatus
from .exceptions import DuplicateWebhookError
from .models import CollectionIntent, PaymentEvent, PayoutBatch, PayoutInstruction, VirtualAccount
from .serializers import (
    CollectionIntentSerializer,
    PaymentEventSerializer,
    PayoutBatchSerializer,
    PayoutBatchSummarySerializer,
    PayoutInstructionSerializer,
    VirtualAccountSerializer,
)


def _entity_obj(entity, model, ref, field):
    """Fetch ``model`` within ``entity`` by numeric pk, or by ``code`` for models
    that have one (so the UI pickers, which emit codes, resolve too). Raises a
    400 ValidationError when nothing matches."""
    if ref in (None, ""):
        return None
    qs = model.objects.filter(entity=entity)
    has_code = any(getattr(f, "name", None) == "code" for f in model._meta.get_fields())
    obj = None
    if str(ref).isdigit():
        obj = qs.filter(pk=ref).first()
    # Account (and other) codes are themselves numeric strings, so a digit ref may
    # be a *code*, not a pk — fall back to a code match before giving up.
    if obj is None and has_code:
        obj = qs.filter(code__iexact=str(ref)).first()
    if obj is None:
        raise ValidationError({field: f"No {model.__name__.lower()} '{ref}' in this entity."})
    return obj


# --------------------------------------------------------------------------- #
# Collections                                                                 #
# --------------------------------------------------------------------------- #

class CollectionListCreateView(APIView):
    """GET (list) / POST (initiate) collections for an entity.

    docstring-name: Collections
    """

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
        if (va := request.query_params.get("virtual_account")):
            qs = qs.filter(virtual_account_id=va)
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
    """GET a collection; ``?verify=1`` polls the provider and confirms if settled.

    docstring-name: Collections
    """

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


class VirtualAccountListCreateView(APIView):
    """GET (list) / POST (provision) dedicated virtual accounts for an entity.

    GET is paginated with filters (``status``, ``provider``, ``customer``,
    ``search``) and rides KPI counts (active / inactive / providers in use) in
    the envelope. The funding number/name stay FLS-stripped unless the caller
    holds ``payments.virtual_account.view_sensitive``.

    docstring-name: Virtual accounts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.virtual_account.create" if self.request.method == "POST" \
            else "payments.virtual_account.view"

    def get(self, request):
        entity = resolve_entity(request)
        base = VirtualAccount.objects.filter(entity=entity)
        kpis = {
            "total": base.count(),
            "active": base.filter(status=VirtualAccountStatus.ACTIVE).count(),
            "inactive": base.filter(status=VirtualAccountStatus.INACTIVE).count(),
            "providers": base.values("provider").distinct().count(),
        }
        qs = base.select_related("customer", "deposit_account", "currency")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_.upper())
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider.upper())
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer__code__iexact=customer)
        if (search := request.query_params.get("search")):
            qs = qs.filter(
                Q(customer__name__icontains=search) | Q(customer__code__icontains=search)
                | Q(account_number__icontains=search) | Q(bank_name__icontains=search))
        qs = qs.order_by("-created_at")

        page = max(int(request.query_params.get("page", 1) or 1), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20) or 20), 1), 100)
        total = qs.count()
        total_pages = math.ceil(total / page_size) if total else 1
        start = (page - 1) * page_size
        rows = VirtualAccountSerializer(
            qs[start:start + page_size], many=True, context={"request": request}).data
        return Response({
            "success": True,
            "message": "Virtual accounts retrieved.",
            "pagination": {
                "currentPage": page, "pageSize": page_size, "totalItems": total,
                "totalPages": total_pages, "next": None, "previous": None,
            },
            "kpis": kpis,
            "data": rows,
        })

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
            "Virtual account created.",
            data=VirtualAccountSerializer(va, context={"request": request}).data, status=201,
        )


class VirtualAccountDetailView(APIView):
    """GET one virtual account, or PATCH its status (activate / deactivate).

    docstring-name: Virtual accounts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.virtual_account.manage" if self.request.method == "PATCH" \
            else "payments.virtual_account.view"

    def _get(self, request, pk):
        entity = resolve_entity(request)
        va = (VirtualAccount.objects
              .filter(entity=entity, pk=pk)
              .select_related("customer", "deposit_account", "currency").first())
        if va is None:
            raise NotFound("No virtual account matches this id for the entity.")
        return entity, va

    def get(self, request, pk):
        _, va = self._get(request, pk)
        return success_response(
            "Virtual account retrieved.",
            data=VirtualAccountSerializer(va, context={"request": request}).data)

    def patch(self, request, pk):
        _, va = self._get(request, pk)
        status_ = str(request.data.get("status", "")).upper()
        services.set_virtual_account_status(va, status=status_, actor_user=request.user)
        return success_response(
            f"Virtual account {va.status.lower()}.",
            data=VirtualAccountSerializer(va, context={"request": request}).data)


# --------------------------------------------------------------------------- #
# Payouts                                                                     #
# --------------------------------------------------------------------------- #

class PayoutListCreateView(APIView):
    """GET (list) / POST (initiate) payouts for an entity.

    docstring-name: Payouts
    """

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
        # A payout settles a vendor's payable, so a vendor is required (it is what
        # confirmation books — Dr the vendor's AP control / Cr bank).
        if not body.get("vendor"):
            raise ValidationError({"vendor": "A payout must be linked to a vendor."})
        from django.db.models import Q
        from vs_procurement.models import Vendor
        raw = str(body.get("vendor"))
        lookup = Q(code=raw) | Q(pk=raw) if raw.isdigit() else Q(code=raw)
        vendor = Vendor.objects.filter(entity=entity).filter(lookup).first()
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


class PayoutBatchListCreateView(APIView):
    """GET (list) / POST (assemble a bulk batch of payouts) for an entity.

    POST creates the batch and its child instructions in ``DRAFT`` — it does **not**
    submit. Pass ``{"submit": true}`` to dispatch immediately after assembly.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.payout.create" if self.request.method == "POST" \
            else "payments.payout.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PayoutBatch.objects.filter(entity=entity)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return success_response(
            "Payout batches retrieved.",
            data=PayoutBatchSummarySerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError({"items": "A non-empty list of payout items is required."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")
        items = []
        for idx, raw in enumerate(raw_items):
            amount = int(raw.get("amount") or 0)
            if amount <= 0:
                raise ValidationError({f"items[{idx}].amount": "A positive amount (kobo) is required."})
            for field in ("beneficiary_name", "beneficiary_account_number"):
                if not raw.get(field):
                    raise ValidationError({f"items[{idx}].{field}": "This field is required."})
            # Each line settles a vendor's payable on confirmation, so a vendor is
            # required (resolved by code or id — the picker emits codes).
            if not raw.get("vendor"):
                raise ValidationError({f"items[{idx}].vendor": "Each line must be linked to a vendor."})
            from django.db.models import Q
            from vs_procurement.models import Vendor
            vref = str(raw.get("vendor"))
            vlookup = Q(code=vref) | Q(pk=vref) if vref.isdigit() else Q(code=vref)
            vendor = Vendor.objects.filter(entity=entity).filter(vlookup).first()
            if vendor is None:
                raise ValidationError({f"items[{idx}].vendor": "No such vendor in this entity."})
            items.append({
                "amount": amount, "beneficiary_name": raw["beneficiary_name"],
                "beneficiary_account_number": raw["beneficiary_account_number"],
                "beneficiary_bank_code": raw.get("beneficiary_bank_code", ""),
                "vendor": vendor, "narration": raw.get("narration", ""),
                "wht_amount": int(raw.get("wht_amount") or 0),
                "metadata": raw.get("metadata") or {},
            })
        batch = services.create_payout_batch(
            entity=entity, items=items, provider=body.get("provider"),
            source_account=source, title=body.get("title", ""),
            narration=body.get("narration", ""), actor_user=request.user,
        )
        if body.get("submit") in (True, "1", "true", "True"):
            batch = services.submit_payout_batch(batch, actor_user=request.user)
        return success_response(
            "Payout batch created.", data=PayoutBatchSerializer(batch).data, status=201,
        )


class PayoutBatchDetailView(APIView):
    """GET a batch with its items; POST submits the batch's pending instructions.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.payout.create" if self.request.method == "POST" \
            else "payments.payout.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()
        if batch is None:
            raise NotFound("No such payout batch in this entity.")
        return success_response(
            "Payout batch retrieved.", data=PayoutBatchSerializer(batch).data,
        )

    def post(self, request, pk):
        entity = resolve_entity(request)
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()
        if batch is None:
            raise NotFound("No such payout batch in this entity.")
        batch = services.submit_payout_batch(batch, actor_user=request.user)
        return success_response(
            "Payout batch submitted.", data=PayoutBatchSerializer(batch).data,
        )


# --------------------------------------------------------------------------- #
# Settlement reconciliation (read-side report)                                #
# --------------------------------------------------------------------------- #

class SettlementReconciliationView(APIView):
    """GET a settlement reconciliation of gateway records vs. imported bank lines.

    Query: ``?entity=``, optional ``?start_date=&end_date=`` (YYYY-MM-DD, inclusive) and
    ``?provider=``.

    docstring-name: Settlement reconciliation
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.report.view"

    def get(self, request):
        import datetime

        entity = resolve_entity(request)

        def _date(name):
            raw = request.query_params.get(name)
            if not raw:
                return None
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError:
                raise ValidationError({name: "Expected an ISO date (YYYY-MM-DD)."})

        recon = reconciliation.settlement_reconciliation(
            entity, start_date=_date("start_date"), end_date=_date("end_date"),
            provider=request.query_params.get("provider"),
        )
        data = {
            "entity_code": recon.entity_code,
            "start_date": recon.start_date.isoformat() if recon.start_date else None,
            "end_date": recon.end_date.isoformat() if recon.end_date else None,
            "provider": recon.provider,
            "is_reconciled": recon.is_reconciled,
            "summary": {
                "settled_count": recon.settled_count,
                "unsettled_count": recon.unsettled_count,
                "gateway_total": recon.gateway_total,
                "settled_total": recon.settled_total,
                "unsettled_total": recon.unsettled_total,
                "unmatched_bank_total": recon.unmatched_bank_total,
                "unmatched_bank_count": len(recon.unmatched_bank_lines),
            },
            "rows": [
                {
                    "kind": r.kind, "gateway_id": r.gateway_id, "reference": r.reference,
                    "provider": r.provider, "provider_reference": r.provider_reference,
                    "amount": r.amount, "amount_naira": r.amount_naira,
                    "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
                    "settled": r.settled, "match_basis": r.match_basis,
                    "matched_bank_line_id": r.matched_bank_line_id,
                    "settled_amount": r.settled_amount, "fee_amount": r.fee_amount,
                    "settlement_reference": r.settlement_reference,
                    "settlement_date": r.settlement_date.isoformat() if r.settlement_date else None,
                    "settlement_description": r.settlement_description,
                }
                for r in recon.rows
            ],
            "unmatched_bank_lines": [
                {
                    "bank_line_id": b.bank_line_id, "bank_account_id": b.bank_account_id,
                    "txn_date": b.txn_date.isoformat(), "description": b.description,
                    "reference": b.reference, "amount": b.amount,
                    "amount_naira": b.amount_naira,
                }
                for b in recon.unmatched_bank_lines
            ],
        }
        return success_response("Settlement reconciliation retrieved.", data=data)


class TransactionsLogView(APIView):
    """GET the append-only gateway action log (the transactions log) for an entity.

    Reads :class:`~vs_payments.models.PaymentEvent` — the immutable record of every
    gateway action (collections, payouts, virtual accounts, webhooks) including failed
    and rejected attempts. Filterable by ``?action=``, ``?provider=`` and
    ``?succeeded=true|false``; capped at the most recent 200 rows.

    docstring-name: Transactions log
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PaymentEvent.objects.filter(entity=entity).select_related("actor_user")
        if (action := request.query_params.get("action")):
            qs = qs.filter(action=action)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        succeeded = request.query_params.get("succeeded")
        if succeeded in ("true", "True", "1"):
            qs = qs.filter(succeeded=True)
        elif succeeded in ("false", "False", "0"):
            qs = qs.filter(succeeded=False)
        data = PaymentEventSerializer(qs[:200], many=True).data
        return success_response("Transactions log retrieved.", data=data)


# --------------------------------------------------------------------------- #
# Webhook receiver (public, signature-verified)                               #
# --------------------------------------------------------------------------- #

class WebhookView(APIView):
    """POST /webhooks/<provider>/ — raw signed PSP event. No JWT; signature is the auth.

    docstring-name: PSP webhook receiver
    """

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
