"""REST API for vs_payments.  # Thin read/write API layer for the gateway app.

Two kinds of endpoint:

* **Authenticated, entity-scoped** actions/reads (``?entity=<id|code>``) for initiating
  collections/payouts, provisioning virtual accounts and listing gateway records. These
  use the platform envelope + RBAC (``payments.<resource>.<action>``), exactly like
  ``vs_finance``.  # All tenant-scoped writes and reads go through RBAC.
* A **public webhook receiver** (``/webhooks/<provider>/``) that takes the raw signed
  body from the PSP. It is ``AllowAny`` because the PSP can't carry a JWT — authenticity
  comes from the body signature, verified inside :func:`vs_payments.webhooks.ingest_webhook`.  # Webhooks authenticate by signature, not JWT.

Domain errors raised by the services/webhooks render through the shared typed-exception
handler, so the views stay thin.  # Keep business logic in services, not views.
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.models import Account, Customer, Invoice
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive, user_has_rbac_permission

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


# Support the paginate workflow.
def _paginate(request, qs, serializer_cls, view, **ser_kwargs):
    """Paginate a queryset through the platform's XVSPagination envelope ({pagination, data}).
    Page size is a fixed 25 (override per-request with ?page_size=, capped at 100)."""
    paginator = XVSPagination()  # Build the shared pagination helper.
    paginator.page_size = 25  # Default to 25 rows per page for this API.
    page = paginator.paginate_queryset(qs, request, view=view)  # Slice the queryset for the current page.
    return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Wrap the serialized page.


# Support the entity obj workflow.
def _entity_obj(entity, model, ref, field):
    """Fetch ``model`` within ``entity`` by numeric pk, or by ``code`` for models
    that have one (so the UI pickers, which emit codes, resolve too). Raises a
    400 ValidationError when nothing matches."""
    if ref in (None, ""):  # Blank inputs are allowed to resolve to nothing.
        return None
    qs = model.objects.filter(entity=entity)
    has_code = any(getattr(f, "name", None) == "code" for f in model._meta.get_fields())  # Check whether the model exposes a code field.
    obj = None  # Hold the resolved object if any lookup succeeds.
    if str(ref).isdigit():  # Numeric refs might be pks or codes.
        obj = qs.filter(pk=ref).first()
    # Account (and other) codes are themselves numeric strings, so a digit ref may
    # be a *code*, not a pk — fall back to a code match before giving up.  # Handle numeric codes defensively.
    if obj is None and has_code:  # Only try a code lookup when the model supports one.
        obj = qs.filter(code__iexact=str(ref)).first()
    if obj is None:  # Nothing matched the entity-scoped lookup.
        raise ValidationError({field: f"No {model.__name__.lower()} '{ref}' in this entity."})
    return obj  # Return the resolved object.


# --------------------------------------------------------------------------- #
# Collections                                                                 #
# --------------------------------------------------------------------------- #

# Console status groups → underlying CollectionStatus values (the UI filters by group).  # Keep UI filters aligned with storage.
COLLECTION_GROUPS = {
    "PENDING": ["PENDING", "PROCESSING"],
    "PAID": ["SUCCEEDED"],
    "FAILED": ["FAILED", "ABANDONED"],
    "REFUNDED": ["REFUNDED"],
}


# Group endpoint behavior for Collection List Create View.
class CollectionListCreateView(APIView):
    """GET (list) / POST (initiate) collections for an entity.

    docstring-name: Collections
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Authenticated tenant users with RBAC only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        # POST (initiate) needs the stronger 'create'; GET (list) needs only 'view'.  # Split read/write permission.
        return "payments.collection.create" if self.request.method == "POST" \
            else "payments.collection.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity from the request.
        qs = CollectionIntent.objects.filter(entity=entity).select_related(
            "customer", "payment",
        )
        if (group := request.query_params.get("group")) in COLLECTION_GROUPS:
            qs = qs.filter(status__in=COLLECTION_GROUPS[group])
        elif (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        if (va := request.query_params.get("virtual_account")):
            qs = qs.filter(virtual_account_id=va)
        return _paginate(request, qs.order_by("-created_at", "-id"), CollectionIntentSerializer, self)

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)  # Resolve the entity before creating the intent.
        body = request.data  # Read the posted payload once.
        amount = int(body.get("amount") or 0)
        if amount <= 0:  # Reject empty or negative collections.
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        customer = _entity_obj(entity, Customer, body.get("customer"), "customer")
        invoice = _entity_obj(entity, Invoice, body.get("invoice"), "invoice")
        deposit = _entity_obj(entity, Account, body.get("deposit_account"), "deposit_account")
        intent = services.initiate_collection(  # Hand off to the business service for PSP initiation.
            entity=entity, amount=amount, customer=customer, invoice=invoice,
            deposit_account=deposit, channel=body.get("channel"),
            provider=body.get("provider"), payer_email=body.get("payer_email", ""),
            payer_name=body.get("payer_name", ""), narration=body.get("narration", ""),
            metadata=body.get("metadata") or {}, actor_user=request.user,
        )
        return success_response(
            "Collection initiated.", data=CollectionIntentSerializer(intent).data, status=201,
        )


# Group endpoint behavior for Collection Summary View.
class CollectionSummaryView(APIView):
    """GET /payments/collections/summary/ — KPI totals (kobo) + status-group counts over
    ALL rows, so the header stays accurate while the list paginates. Honors ?provider.

    docstring-name: Collections summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.collection.view"  # Collections summary uses view permission only.

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        entity = resolve_entity(request)  # Scope the summary to the current entity.
        qs = CollectionIntent.objects.filter(entity=entity)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        g = COLLECTION_GROUPS  # Short alias for the status groups.
        agg = qs.aggregate(
            total=Count("id"),
            collected=Coalesce(Sum("amount", filter=Q(status__in=g["PAID"])), 0),
            pending=Coalesce(Sum("amount", filter=Q(status__in=g["PENDING"])), 0),
            failed=Coalesce(Sum("amount", filter=Q(status__in=g["FAILED"])), 0),
            paid_c=Count("id", filter=Q(status__in=g["PAID"])),
            pending_c=Count("id", filter=Q(status__in=g["PENDING"])),
            failed_c=Count("id", filter=Q(status__in=g["FAILED"])),
            refunded_c=Count("id", filter=Q(status__in=g["REFUNDED"])),
        )
        terminal = agg["paid_c"] + agg["failed_c"]  # Only terminal outcomes belong in the success-rate denominator.
        rate = round(agg["paid_c"] * 100 / terminal) if terminal else None  # Compute a simple success rate when possible.
        return success_response("Collections summary retrieved.", data={
            "total": agg["total"],
            "collected": {"kobo": agg["collected"], "naira": format_naira(agg["collected"])},
            "pending": {"kobo": agg["pending"], "naira": format_naira(agg["pending"])},
            "failed": {"kobo": agg["failed"], "naira": format_naira(agg["failed"])},
            "success_rate": rate,
            "group_counts": {
                "PAID": agg["paid_c"], "PENDING": agg["pending_c"],
                "FAILED": agg["failed_c"], "REFUNDED": agg["refunded_c"],
            },
        })


# Group endpoint behavior for Collection Detail View.
class CollectionDetailView(APIView):
    """GET a collection; ``?verify=1`` polls the provider and confirms if settled.

    docstring-name: Collections
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-scoped read access.
    rbac_permission = "payments.collection.view"  # View permission is enough to read/verify.

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        intent = CollectionIntent.objects.filter(entity=entity, pk=pk).first()
        if intent is None:  # Return 404 when the record does not exist in this tenant.
            raise NotFound("No such collection in this entity.")
        if request.query_params.get("verify") in ("1", "true", "True"):
            intent = services.confirm_collection(intent, actor_user=request.user)  # Confirm against the provider before returning.
        return success_response("Collection retrieved.", data=CollectionIntentSerializer(intent).data)


# Group endpoint behavior for Virtual Account List Create View.
class VirtualAccountListCreateView(APIView):
    """GET (list) / POST (provision) dedicated virtual accounts for an entity.

    GET is paginated with filters (``status``, ``provider``, ``customer``,
    ``search``) and rides KPI counts (active / inactive / providers in use) in
    the envelope. The funding number/name stay FLS-stripped unless the caller
    holds ``payments.virtual_account.view_sensitive``.

    docstring-name: Virtual accounts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Authenticated tenant access only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return (  # Use create permission for POST, view permission otherwise.
            "payments.virtual_account.create"
            if self.request.method == "POST"
            else "payments.virtual_account.view"
        )

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        base = VirtualAccount.objects.filter(entity=entity)
        kpis = {  # Compute the summary KPIs used by the list header.
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
        resp = _paginate(request, qs.order_by("-created_at"), VirtualAccountSerializer, self,
                         context={"request": request})
        resp.data["kpis"] = kpis  # Attach KPI data to the pagination envelope.
        return resp  # Return the paginated response.

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        customer = _entity_obj(entity, Customer, request.data.get("customer"), "customer")
        if customer is None:  # Virtual accounts are always customer-specific in this flow.
            raise ValidationError({"customer": "A customer is required."})
        deposit = _entity_obj(entity, Account, request.data.get("deposit_account"), "deposit_account")
        va = services.create_virtual_account(  # Delegate provisioning to the service layer.
            entity=entity, customer=customer, provider=request.data.get("provider"),
            deposit_account=deposit, bank_code=request.data.get("bank_code", ""),
            actor_user=request.user,
        )
        return success_response(
            "Virtual account created.",
            data=VirtualAccountSerializer(va, context={"request": request}).data, status=201,
        )


# Group endpoint behavior for Virtual Account Detail View.
class VirtualAccountDetailView(APIView):
    """GET one virtual account, or PATCH its status (activate / deactivate).

    docstring-name: Virtual accounts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return (  # Use manage permission for PATCH, view permission otherwise.
            "payments.virtual_account.manage"
            if self.request.method == "PATCH"
            else "payments.virtual_account.view"
        )

    # Support the get workflow.
    def _get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        va = (VirtualAccount.objects
              .filter(entity=entity, pk=pk)
              .select_related("customer", "deposit_account", "currency").first())
        if va is None:  # Return 404 when the record doesn't belong to this entity.
            raise NotFound("No virtual account matches this id for the entity.")
        return entity, va  # Return the resolved pair for reuse by GET/PATCH.

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, va = self._get(request, pk)  # Reuse the shared entity-scoped lookup.
        return success_response(
            "Virtual account retrieved.",
            data=VirtualAccountSerializer(va, context={"request": request}).data)

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        _, va = self._get(request, pk)  # Fetch the account first.
        status_ = str(request.data.get("status", "")).upper()
        services.set_virtual_account_status(va, status=status_, actor_user=request.user)  # Delegate the lifecycle change.
        return success_response(
            f"Virtual account {va.status.lower()}.",
            data=VirtualAccountSerializer(va, context={"request": request}).data)


# --------------------------------------------------------------------------- #
# Payouts                                                                     #
# --------------------------------------------------------------------------- #

# Console status groups → underlying PayoutStatus values (PAID shows as "Settled").
PAYOUT_GROUPS = {
    "PENDING": ["PENDING", "PROCESSING"],
    "PAID": ["PAID"],
    "FAILED": ["FAILED", "REVERSED"],
}


# Group endpoint behavior for Payout List Create View.
class PayoutListCreateView(APIView):
    """GET (list) / POST (initiate) payouts for an entity.

    docstring-name: Payouts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return (  # POST needs create permission; GET needs view permission.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutInstruction.objects.filter(entity=entity)
        if (group := request.query_params.get("group")) in PAYOUT_GROUPS:
            qs = qs.filter(status__in=PAYOUT_GROUPS[group])
        elif (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        return _paginate(request, qs.order_by("-created_at", "-id"), PayoutInstructionSerializer, self)

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        body = request.data  # Read the incoming payload once.
        amount = int(body.get("amount") or 0)
        if amount <= 0:  # Reject invalid payout amounts.
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        for field in ("beneficiary_name", "beneficiary_account_number"):  # Validate the required beneficiary fields.
            if not body.get(field):
                raise ValidationError({field: "This field is required."})
        # A payout settles a vendor's payable, so a vendor is required (it is what
        # confirmation books — Dr the vendor's AP control / Cr bank).  # Vendor is needed for AP posting.
        if not body.get("vendor"):
            raise ValidationError({"vendor": "A payout must be linked to a vendor."})
        from django.db.models import Q
        from vs_procurement.models import Vendor
        raw = str(body.get("vendor"))
        lookup = Q(code=raw) | Q(pk=raw) if raw.isdigit() else Q(code=raw)
        vendor = Vendor.objects.filter(entity=entity).filter(lookup).first()
        if vendor is None:  # Reject unknown vendors.
            raise ValidationError({"vendor": "No such vendor in this entity."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")
        payout = services.initiate_payout(  # Delegate to the payment service layer.
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


# Group endpoint behavior for Payout Summary View.
class PayoutSummaryView(APIView):
    """GET /payments/payouts/summary/ — KPI totals + status-group counts over ALL rows.
    Honors ?provider.

    docstring-name: Payouts summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.payout.view"  # Payout summary is view-only.

    # Handle GET requests for this endpoint.
    def get(self, request):
        import datetime

        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        entity = resolve_entity(request)  # Scope the summary to the current entity.
        qs = PayoutInstruction.objects.filter(entity=entity)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        cutoff = timezone.now() - datetime.timedelta(days=7)
        agg = qs.aggregate(
            total=Count("id"),
            settled7d=Coalesce(Sum("amount", filter=Q(status="PAID", confirmed_at__gte=cutoff)), 0),
            pending=Coalesce(Sum("amount", filter=Q(status__in=PAYOUT_GROUPS["PENDING"])), 0),
            paid_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["PAID"])),
            pending_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["PENDING"])),
            failed_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["FAILED"])),
        )
        return success_response("Payouts summary retrieved.", data={
            "total": agg["total"],
            "settled7d": {"kobo": agg["settled7d"], "naira": format_naira(agg["settled7d"])},
            "pending": {"kobo": agg["pending"], "naira": format_naira(agg["pending"])},
            "failed": agg["failed_c"],
            "group_counts": {"PAID": agg["paid_c"], "PENDING": agg["pending_c"], "FAILED": agg["failed_c"]},
        })


# Group endpoint behavior for Payout Batch List Create View.
class PayoutBatchListCreateView(APIView):
    """GET (list) / POST (assemble a bulk batch of payouts) for an entity.

    POST creates the batch and its child instructions in ``DRAFT`` — it does **not**
    submit. Pass ``{"submit": true}`` to dispatch immediately after assembly.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return (  # POST needs create permission; GET needs view permission.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutBatch.objects.filter(entity=entity)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return _paginate(request, qs.order_by("-created_at", "-id"), PayoutBatchSummarySerializer, self)

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        body = request.data  # Read the incoming batch payload.
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:  # Require at least one item.
            raise ValidationError({"items": "A non-empty list of payout items is required."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")
        items = []  # Build the normalized batch items here.
        for idx, raw in enumerate(raw_items):  # Normalize each submitted line item.
            amount = int(raw.get("amount") or 0)
            if amount <= 0:  # Reject empty or negative line amounts.
                raise ValidationError({f"items[{idx}].amount": "A positive amount (kobo) is required."})
            for field in ("beneficiary_name", "beneficiary_account_number"):  # Validate beneficiary fields per line.
                if not raw.get(field):
                    raise ValidationError({f"items[{idx}].{field}": "This field is required."})
            # Each line settles a vendor's payable on confirmation, so a vendor is
            # required (resolved by code or id — the picker emits codes).  # Vendor drives the AP posting later.
            if not raw.get("vendor"):
                raise ValidationError({f"items[{idx}].vendor": "Each line must be linked to a vendor."})
            from django.db.models import Q
            from vs_procurement.models import Vendor
            vref = str(raw.get("vendor"))
            vlookup = Q(code=vref) | Q(pk=vref) if vref.isdigit() else Q(code=vref)
            vendor = Vendor.objects.filter(entity=entity).filter(vlookup).first()
            if vendor is None:  # Reject unknown vendors.
                raise ValidationError({f"items[{idx}].vendor": "No such vendor in this entity."})
            items.append({
                "amount": amount,  # Normalized line amount.
                "beneficiary_name": raw["beneficiary_name"],  # Beneficiary display name.
                "beneficiary_account_number": raw["beneficiary_account_number"],  # Beneficiary account number.
                "beneficiary_bank_code": raw.get("beneficiary_bank_code", ""),
                "vendor": vendor,  # Resolved vendor object.
                "narration": raw.get("narration", ""),
                "wht_amount": int(raw.get("wht_amount") or 0),
                "metadata": raw.get("metadata") or {},
            })  # Keep the normalized payout item.
        batch = services.create_payout_batch(  # Assemble the draft batch in the service layer.
            entity=entity, items=items, provider=body.get("provider"),
            source_account=source, title=body.get("title", ""),
            narration=body.get("narration", ""), actor_user=request.user,
        )
        # Direct submit only when approval is NOT gated for this batch. When a
        # payments.payout_batch template exists, submit=true is ignored and the batch
        # is left DRAFT to be routed via /payout-batches/<id>/submit-for-approval/.
        from vs_finance.approvals import approval_required
        wants_submit = body.get("submit") in (True, "1", "true", "True")
        gated = approval_required(batch)  # True iff a workflow template exists for the batch's scope.
        if wants_submit and not gated:
            batch = services.submit_payout_batch(batch, actor_user=request.user)  # Submit the draft batch immediately.
        message = (  # Tell the caller whether they still need to route it for approval.
            "Payout batch created; submit it for approval."
            if (wants_submit and gated) else "Payout batch created."
        )
        return success_response(
            message, data=PayoutBatchSerializer(batch).data, status=201,
        )


# Group endpoint behavior for Payout Batch Summary View.
class PayoutBatchSummaryView(APIView):
    """GET /payments/payout-batches/summary/ — batch KPI totals over ALL rows.

    docstring-name: Payout batches summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.payout.view"  # View permission is enough for batch summaries.

    # Handle GET requests for this endpoint.
    def get(self, request):
        import datetime

        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutBatch.objects.filter(entity=entity)
        cutoff = timezone.now() - datetime.timedelta(days=7)
        agg = qs.aggregate(
            total=Count("id"),
            queued=Coalesce(Sum("total_amount", filter=Q(status__in=["DRAFT", "PROCESSING"])), 0),
            completed7d=Count("id", filter=Q(status="COMPLETED", submitted_at__gte=cutoff)),
            drafts=Count("id", filter=Q(status="DRAFT")),
        )
        return success_response("Payout batches summary retrieved.", data={
            "total": agg["total"],
            "queued": {"kobo": agg["queued"], "naira": format_naira(agg["queued"])},
            "completed7d": agg["completed7d"],
            "drafts": agg["drafts"],
        })


# Group endpoint behavior for Payout Batch Detail View.
class PayoutBatchDetailView(APIView):
    """GET a batch with its items; POST submits the batch's pending instructions.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return (  # POST submits a batch; GET only views it.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()
        if batch is None:  # Return 404 when the batch does not belong to this tenant.
            raise NotFound("No such payout batch in this entity.")
        return success_response(
            "Payout batch retrieved.", data=PayoutBatchSerializer(batch).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_finance.approvals import approval_required

        entity = resolve_entity(request)  # Resolve the tenant entity.
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()
        if batch is None:  # Return 404 when the batch does not belong to this tenant.
            raise NotFound("No such payout batch in this entity.")
        if approval_required(batch):  # Direct submit is refused once approval is gated.
            raise ValidationError({
                "detail": "This payout batch is approval-gated; submit it for approval "
                          "instead of submitting directly.",
            })
        batch = services.submit_payout_batch(batch, actor_user=request.user)  # Submit pending instructions.
        return success_response(
            "Payout batch submitted.", data=PayoutBatchSerializer(batch).data,
        )


# Group endpoint behavior for Payout Batch Submit-For-Approval View.
class PayoutBatchSubmitForApprovalView(APIView):
    """POST /payments/payout-batches/<id>/submit-for-approval/ — route a batch through approval.

    Hands the batch to the vs_workflow engine; the handler's ``validate_document``
    runs the submit preflight (draft batch with pending instructions) and records the
    batch as awaiting approval. The provider submission fires only on final approval.

    docstring-name: Submit a payout batch for approval
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.
    rbac_permission = "payments.payout_batch.submit"  # Distinct submit-for-approval key.

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_workflow.services.submission import submit_for_approval

        entity = resolve_entity(request)  # Resolve the tenant entity.
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()
        if batch is None:  # Return 404 when the batch does not belong to this tenant.
            raise NotFound("No such payout batch in this entity.")
        submit_for_approval(batch, requested_by=request.user)  # Create the workflow instance + activate stage 1.
        batch.refresh_from_db()  # Pick up the handler's metadata change.
        return success_response(
            "Payout batch submitted for approval.", data=PayoutBatchSerializer(batch).data,
        )


# --------------------------------------------------------------------------- #
# Settlement reconciliation (read-side report)                                #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Settlement Reconciliation View.
class SettlementReconciliationView(APIView):
    """GET a settlement reconciliation of gateway records vs. imported bank lines.

    Query: ``?entity=``, optional ``?start_date=&end_date=`` (YYYY-MM-DD, inclusive) and
    ``?provider=``.

    docstring-name: Settlement reconciliation
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        import datetime

        entity = resolve_entity(request)  # Resolve the tenant entity.

        # Support the date workflow.
        def _date(name):
            raw = request.query_params.get(name)
            if not raw:  # Missing dates stay unset.
                return None
            try:  # Parse ISO dates only.
                return datetime.date.fromisoformat(raw)
            except ValueError:  # Surface a clear validation error for bad input.
                raise ValidationError({name: "Expected an ISO date (YYYY-MM-DD)."})

        recon = reconciliation.settlement_reconciliation(  # Build the read-only reconciliation snapshot.
            entity, start_date=_date("start_date"), end_date=_date("end_date"),
            provider=request.query_params.get("provider"),
        )
        data = {  # Convert the dataclass into a JSON-safe response payload.
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
                for r in recon.rows  # Iterate through the relevant records.
            ],
            "unmatched_bank_lines": [
                {
                    "bank_line_id": b.bank_line_id, "bank_account_id": b.bank_account_id,
                    "txn_date": b.txn_date.isoformat(), "description": b.description,
                    "reference": b.reference, "amount": b.amount,
                    "amount_naira": b.amount_naira,
                }
                for b in recon.unmatched_bank_lines  # Iterate through the relevant records.
            ],
        }
        return success_response("Settlement reconciliation retrieved.", data=data)


# Group endpoint behavior for Transactions Log View.
class TransactionsLogView(APIView):
    """GET the append-only gateway action log (the transactions log) for an entity.

    Reads :class:`~vs_payments.models.PaymentEvent` — the immutable record of every
    gateway action (collections, payouts, virtual accounts, webhooks) including failed
    and rejected attempts. Filterable by ``?action=``, ``?provider=`` and
    ``?succeeded=true|false``; paginated.

    docstring-name: Transactions log
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PaymentEvent.objects.filter(entity=entity).select_related("actor_user")
        if (action := request.query_params.get("action")):
            qs = qs.filter(action=action)
        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        succeeded = request.query_params.get("succeeded")
        if succeeded in ("true", "True", "1"):  # Explicitly request successful events.
            qs = qs.filter(succeeded=True)
        elif succeeded in ("false", "False", "0"):  # Explicitly request failed events.
            qs = qs.filter(succeeded=False)
        return _paginate(request, qs.order_by("-created_at", "-id"), PaymentEventSerializer, self)


# --------------------------------------------------------------------------- #
# Movements — unified money-in (collections) + money-out (payouts) feed        #
# --------------------------------------------------------------------------- #

# Unified status groups across both gateways (collections + payouts).  # Shared movement filters.
MOVEMENT_GROUPS = {
    "SETTLED": (["SUCCEEDED"], ["PAID"]),
    "PENDING": (["PENDING", "PROCESSING"], ["PENDING", "PROCESSING"]),
    "FAILED": (["FAILED", "ABANDONED"], ["FAILED", "REVERSED"]),
    "REFUNDED": (["REFUNDED"], []),
}
_MOVEMENT_COLS = [  # Common projection shape for the movements feed.
    "kind", "gateway_id", "reference", "created_at", "direction", "party", "provider",
    "amount", "status", "narration", "provider_reference", "confirmed_at", "linked_id",
    "email", "account_code", "account_name", "beneficiary_account",
]


# Support the movement querysets workflow.
def _movement_querysets(entity, *, provider=None, group=None):
    """The collection (in) + payout (out) value-querysets projected to a common shape."""
    from django.db.models import CharField, F, Value
    from django.db.models.functions import Coalesce

    cols = CollectionIntent.objects.filter(entity=entity)
    pos = PayoutInstruction.objects.filter(entity=entity)
    if provider:  # Optional PSP filter applied to both sides.
        cols = cols.filter(provider=provider)
        pos = pos.filter(provider=provider)
    if group in MOVEMENT_GROUPS:  # Optional status-group filter.
        c_st, p_st = MOVEMENT_GROUPS[group]  # Split the collection and payout status sets.
        cols = cols.filter(status__in=c_st) if c_st else cols.none()
        pos = pos.filter(status__in=p_st) if p_st else pos.none()

    cv = cols.annotate(
        kind=Value("collection", output_field=CharField()), gateway_id=F("id"),
        direction=Value("in", output_field=CharField()),
        party=Coalesce(F("customer__name"), F("payer_name"), Value(""), output_field=CharField()),
        linked_id=F("payment_id"), email=F("payer_email"),
        account_code=F("deposit_account__code"), account_name=F("deposit_account__name"),
        beneficiary_account=Value("", output_field=CharField()),
    ).values(*_MOVEMENT_COLS)
    pv = pos.annotate(
        kind=Value("payout", output_field=CharField()), gateway_id=F("id"),
        direction=Value("out", output_field=CharField()), party=F("beneficiary_name"),
        linked_id=F("vendor_payment_id"), email=Value("", output_field=CharField()),
        account_code=F("source_account__code"), account_name=F("source_account__name"),
        beneficiary_account=F("beneficiary_account_number"),
    ).values(*_MOVEMENT_COLS)
    return cv, pv  # Return both common-shape querysets for the feed.


# Group endpoint behavior for Movements View.
class MovementsView(APIView):
    """GET /payments/movements/ — unified, paginated money-movement feed: confirmed-or-
    pending collections (in) + payouts (out), newest first. Filters: ``?direction=in|out``,
    ``?group=SETTLED|PENDING|FAILED|REFUNDED``, ``?provider=``. Payout beneficiary
    name/account are FLS-masked without payments.payout.view_sensitive.

    docstring-name: Movements feed
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        provider = request.query_params.get("provider")
        group = request.query_params.get("group")
        direction = request.query_params.get("direction")
        cv, pv = _movement_querysets(entity, provider=provider, group=group)  # Build the projected querysets.

        parts = []  # Collect whichever sides the caller requested.
        if direction != "out":  # Include collections unless the caller asked for payouts only.
            parts.append(cv)  # Add the collection queryset.
        if direction != "in":  # Include payouts unless the caller asked for collections only.
            parts.append(pv)  # Add the payout queryset.
        union = parts[0] if len(parts) == 1 else parts[0].union(parts[1], all=True)  # Union both sides when needed.
        union = union.order_by("-created_at")

        paginator = XVSPagination()  # Build the shared paginator.
        page = paginator.paginate_queryset(union, request, view=self)  # Slice the union query.
        can_sensitive = user_has_rbac_permission(request.user, "payments.payout.view_sensitive")  # Check for sensitive access.
        rows = []  # Build the response rows explicitly so we can mask sensitive payout data.
        for m in page:  # Convert each result row into a serializable mapping.
            row = dict(m)  # Coerce the projected row into a plain dict.
            if row["kind"] == "payout" and not can_sensitive:  # Mask payout beneficiary details without the grant.
                row["party"] = "••••"  # Hide beneficiary name.
                row["beneficiary_account"] = "••••"  # Hide beneficiary account number.
            row["amount_naira"] = format_naira(row["amount"])  # Add a display amount.
            row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None  # Normalize timestamps.
            row["confirmed_at"] = row["confirmed_at"].isoformat() if row["confirmed_at"] else None  # Normalize timestamps.
            rows.append(row)  # Accumulate the row.
        return paginator.get_paginated_response(rows)  # Return the paginated feed.


# Group endpoint behavior for Movements Summary View.
class MovementsSummaryView(APIView):
    """GET /payments/movements/summary/ — money-in (7d) / money-out (7d) / pending / failed
    across both gateways, for the Transactions Log header.

    docstring-name: Movements summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        import datetime

        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        entity = resolve_entity(request)  # Resolve the tenant entity.
        provider = request.query_params.get("provider")
        cols = CollectionIntent.objects.filter(entity=entity)
        pos = PayoutInstruction.objects.filter(entity=entity)
        if provider:  # Apply the provider filter to both sides when requested.
            cols = cols.filter(provider=provider)
            pos = pos.filter(provider=provider)
        cutoff = timezone.now() - datetime.timedelta(days=7)
        c = cols.aggregate(
            in7d=Coalesce(Sum("amount", filter=Q(status="SUCCEEDED", confirmed_at__gte=cutoff)), 0),
            pending=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["PENDING"][0])),
            failed=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["FAILED"][0])),
        )
        p = pos.aggregate(
            out7d=Coalesce(Sum("amount", filter=Q(status="PAID", confirmed_at__gte=cutoff)), 0),
            pending=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["PENDING"][1])),
            failed=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["FAILED"][1])),
        )
        return success_response("Movements summary retrieved.", data={
            "in7d": {"kobo": c["in7d"], "naira": format_naira(c["in7d"])},
            "out7d": {"kobo": p["out7d"], "naira": format_naira(p["out7d"])},
            "pending": c["pending"] + p["pending"],
            "failed": c["failed"] + p["failed"],
        })


# --------------------------------------------------------------------------- #
# Webhook receiver (public, signature-verified)                               #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Webhook View.
class WebhookView(APIView):
    """POST /webhooks/<provider>/ — raw signed PSP event. No JWT; signature is the auth.

    docstring-name: PSP webhook receiver
    """

    authentication_classes: list = []  # Webhooks authenticate by signature, not session/JWT.
    permission_classes = [AllowAny]  # Public endpoint for PSP callbacks.

    # Handle POST requests for this endpoint.
    def post(self, request, provider):
        try:  # Duplicate events are expected and should be acknowledged.
            event = webhooks.ingest_webhook(  # Hand the raw signed request to the webhook ingestion layer.
                provider=provider, raw_body=request.body, headers=dict(request.headers),
            )
        except DuplicateWebhookError:  # Already processed; acknowledge so the provider stops retrying.
            # Already handled — acknowledge so the provider stops retrying.
            return success_response("Duplicate event ignored.", data={"duplicate": True})
        return success_response(
            "Webhook processed.", data={"id": event.id, "status": event.status},
        )
