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
from __future__ import annotations  # Defer annotation evaluation for forward references.

from django.db.models import Q  # Used for search and vendor lookups.
from django.utils import timezone  # Used for recent-window KPI calculations.
from rest_framework import generics  # Present in this module's imports for DRF patterns.
from rest_framework.exceptions import NotFound, ValidationError  # Standard 404/400 API errors.
from rest_framework.permissions import AllowAny  # Public permission for webhooks.
from rest_framework.views import APIView  # Base class for the lightweight endpoint views.

from core.pagination import XVSPagination  # Shared pagination envelope used across the platform.
from core.response import success_response  # Shared success envelope used by all API endpoints.
from vs_finance.money import format_naira  # Format integer kobo as naira strings.
from vs_finance.models import Account, Customer, Invoice  # Finance models used in payment flows.
from vs_finance.views import resolve_entity  # Resolve the tenant/entity from the request.
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive, user_has_rbac_permission  # RBAC gates.

from . import reconciliation, services, webhooks  # Reconciliation, business services, and webhook ingestion.
from .constants import VirtualAccountStatus  # Local lifecycle enums.
from .exceptions import DuplicateWebhookError  # Raised when a webhook has already been processed.
from .models import CollectionIntent, PaymentEvent, PayoutBatch, PayoutInstruction, VirtualAccount  # Gateway records.
from .serializers import (
    CollectionIntentSerializer,  # Collection read serializer.
    PaymentEventSerializer,  # Gateway audit log serializer.
    PayoutBatchSerializer,  # Batch detail serializer.
    PayoutBatchSummarySerializer,  # Batch list serializer.
    PayoutInstructionSerializer,  # Payout detail serializer.
    VirtualAccountSerializer,  # Virtual account serializer.
)


def _paginate(request, qs, serializer_cls, view, **ser_kwargs):
    """Paginate a queryset through the platform's XVSPagination envelope ({pagination, data}).
    Page size is a fixed 25 (override per-request with ?page_size=, capped at 100)."""
    paginator = XVSPagination()  # Build the shared pagination helper.
    paginator.page_size = 25  # Default to 25 rows per page for this API.
    page = paginator.paginate_queryset(qs, request, view=view)  # Slice the queryset for the current page.
    return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Wrap the serialized page.


def _entity_obj(entity, model, ref, field):
    """Fetch ``model`` within ``entity`` by numeric pk, or by ``code`` for models
    that have one (so the UI pickers, which emit codes, resolve too). Raises a
    400 ValidationError when nothing matches."""
    if ref in (None, ""):  # Blank inputs are allowed to resolve to nothing.
        return None
    qs = model.objects.filter(entity=entity)  # Scope the lookup to the current entity.
    has_code = any(getattr(f, "name", None) == "code" for f in model._meta.get_fields())  # Check whether the model exposes a code field.
    obj = None  # Hold the resolved object if any lookup succeeds.
    if str(ref).isdigit():  # Numeric refs might be pks or codes.
        obj = qs.filter(pk=ref).first()  # Try primary key lookup first.
    # Account (and other) codes are themselves numeric strings, so a digit ref may
    # be a *code*, not a pk — fall back to a code match before giving up.  # Handle numeric codes defensively.
    if obj is None and has_code:  # Only try a code lookup when the model supports one.
        obj = qs.filter(code__iexact=str(ref)).first()  # Match the code case-insensitively.
    if obj is None:  # Nothing matched the entity-scoped lookup.
        raise ValidationError({field: f"No {model.__name__.lower()} '{ref}' in this entity."})  # Surface a field error.
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


class CollectionListCreateView(APIView):
    """GET (list) / POST (initiate) collections for an entity.

    docstring-name: Collections
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Authenticated tenant users with RBAC only.

    @property
    def rbac_permission(self):
        # POST (initiate) needs the stronger 'create'; GET (list) needs only 'view'.  # Split read/write permission.
        return "payments.collection.create" if self.request.method == "POST" \
            else "payments.collection.view"

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity from the request.
        qs = CollectionIntent.objects.filter(entity=entity).select_related(  # Pull the collection records and related links.
            "customer", "payment",
        )
        if (group := request.query_params.get("group")) in COLLECTION_GROUPS:  # Support grouped UI filters.
            qs = qs.filter(status__in=COLLECTION_GROUPS[group])  # Expand group into underlying statuses.
        elif (status_ := request.query_params.get("status")):  # Allow direct status filtering too.
            qs = qs.filter(status=status_)  # Filter by the exact status requested.
        if (provider := request.query_params.get("provider")):  # Optional PSP filter.
            qs = qs.filter(provider=provider)  # Narrow to one provider.
        if (va := request.query_params.get("virtual_account")):  # Optional virtual-account filter.
            qs = qs.filter(virtual_account_id=va)  # Narrow to one virtual account.
        return _paginate(request, qs.order_by("-created_at", "-id"), CollectionIntentSerializer, self)  # Return the paginated result.

    def post(self, request):
        entity = resolve_entity(request)  # Resolve the entity before creating the intent.
        body = request.data  # Read the posted payload once.
        amount = int(body.get("amount") or 0)  # Normalize amount to kobo.
        if amount <= 0:  # Reject empty or negative collections.
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        customer = _entity_obj(entity, Customer, body.get("customer"), "customer")  # Resolve the customer input.
        invoice = _entity_obj(entity, Invoice, body.get("invoice"), "invoice")  # Resolve the optional invoice input.
        deposit = _entity_obj(entity, Account, body.get("deposit_account"), "deposit_account")  # Resolve the optional deposit account.
        intent = services.initiate_collection(  # Hand off to the business service for PSP initiation.
            entity=entity, amount=amount, customer=customer, invoice=invoice,
            deposit_account=deposit, channel=body.get("channel"),
            provider=body.get("provider"), payer_email=body.get("payer_email", ""),
            payer_name=body.get("payer_name", ""), narration=body.get("narration", ""),
            metadata=body.get("metadata") or {}, actor_user=request.user,
        )
        return success_response(  # Return the hydrated intent in the standard success envelope.
            "Collection initiated.", data=CollectionIntentSerializer(intent).data, status=201,
        )


class CollectionSummaryView(APIView):
    """GET /payments/collections/summary/ — KPI totals (kobo) + status-group counts over
    ALL rows, so the header stays accurate while the list paginates. Honors ?provider.

    docstring-name: Collections summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.collection.view"  # Collections summary uses view permission only.

    def get(self, request):
        from django.db.models import Count, Q, Sum  # Aggregate counts and totals in SQL.
        from django.db.models.functions import Coalesce  # Replace NULL aggregates with zero.

        entity = resolve_entity(request)  # Scope the summary to the current entity.
        qs = CollectionIntent.objects.filter(entity=entity)  # Start from all collection intents.
        if (provider := request.query_params.get("provider")):  # Optional PSP filter.
            qs = qs.filter(provider=provider)  # Narrow the aggregation to one provider.
        g = COLLECTION_GROUPS  # Short alias for the status groups.
        agg = qs.aggregate(  # Compute all KPI totals in one query.
            total=Count("id"),  # Total number of collection rows.
            collected=Coalesce(Sum("amount", filter=Q(status__in=g["PAID"])), 0),  # Money collected successfully.
            pending=Coalesce(Sum("amount", filter=Q(status__in=g["PENDING"])), 0),  # Money still pending.
            failed=Coalesce(Sum("amount", filter=Q(status__in=g["FAILED"])), 0),  # Money that failed or was abandoned.
            paid_c=Count("id", filter=Q(status__in=g["PAID"])),  # Count of paid rows.
            pending_c=Count("id", filter=Q(status__in=g["PENDING"])),  # Count of pending rows.
            failed_c=Count("id", filter=Q(status__in=g["FAILED"])),  # Count of failed rows.
            refunded_c=Count("id", filter=Q(status__in=g["REFUNDED"])),  # Count of refunded rows.
        )
        terminal = agg["paid_c"] + agg["failed_c"]  # Only terminal outcomes belong in the success-rate denominator.
        rate = round(agg["paid_c"] * 100 / terminal) if terminal else None  # Compute a simple success rate when possible.
        return success_response("Collections summary retrieved.", data={  # Return the KPI payload.
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


class CollectionDetailView(APIView):
    """GET a collection; ``?verify=1`` polls the provider and confirms if settled.

    docstring-name: Collections
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-scoped read access.
    rbac_permission = "payments.collection.view"  # View permission is enough to read/verify.

    def get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        intent = CollectionIntent.objects.filter(entity=entity, pk=pk).first()  # Look up the collection within the entity.
        if intent is None:  # Return 404 when the record does not exist in this tenant.
            raise NotFound("No such collection in this entity.")
        if request.query_params.get("verify") in ("1", "true", "True"):  # Optional live verification request.
            intent = services.confirm_collection(intent, actor_user=request.user)  # Confirm against the provider before returning.
        return success_response("Collection retrieved.", data=CollectionIntentSerializer(intent).data)  # Return serialized details.


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
    def rbac_permission(self):
        return (  # Use create permission for POST, view permission otherwise.
            "payments.virtual_account.create"
            if self.request.method == "POST"
            else "payments.virtual_account.view"
        )

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        base = VirtualAccount.objects.filter(entity=entity)  # Start from all entity virtual accounts.
        kpis = {  # Compute the summary KPIs used by the list header.
            "total": base.count(),  # Total virtual accounts.
            "active": base.filter(status=VirtualAccountStatus.ACTIVE).count(),  # Active accounts.
            "inactive": base.filter(status=VirtualAccountStatus.INACTIVE).count(),  # Inactive accounts.
            "providers": base.values("provider").distinct().count(),  # Distinct providers in use.
        }
        qs = base.select_related("customer", "deposit_account", "currency")  # Prefetch related display data.
        if (status_ := request.query_params.get("status")):  # Optional status filter.
            qs = qs.filter(status=status_.upper())  # Compare using the enum value.
        if (provider := request.query_params.get("provider")):  # Optional provider filter.
            qs = qs.filter(provider=provider.upper())  # Normalize provider casing.
        if (customer := request.query_params.get("customer")):  # Optional customer filter.
            qs = qs.filter(customer__code__iexact=customer)  # Allow case-insensitive customer code matching.
        if (search := request.query_params.get("search")):  # Optional broad search across display fields.
            qs = qs.filter(
                Q(customer__name__icontains=search) | Q(customer__code__icontains=search)
                | Q(account_number__icontains=search) | Q(bank_name__icontains=search))
        resp = _paginate(request, qs.order_by("-created_at"), VirtualAccountSerializer, self,  # Return the paginated list.
                         context={"request": request})
        resp.data["kpis"] = kpis  # Attach KPI data to the pagination envelope.
        return resp  # Return the paginated response.

    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        customer = _entity_obj(entity, Customer, request.data.get("customer"), "customer")  # Resolve the customer input.
        if customer is None:  # Virtual accounts are always customer-specific in this flow.
            raise ValidationError({"customer": "A customer is required."})
        deposit = _entity_obj(entity, Account, request.data.get("deposit_account"), "deposit_account")  # Optional deposit account.
        va = services.create_virtual_account(  # Delegate provisioning to the service layer.
            entity=entity, customer=customer, provider=request.data.get("provider"),
            deposit_account=deposit, bank_code=request.data.get("bank_code", ""),
            actor_user=request.user,
        )
        return success_response(  # Return the created account in the standard envelope.
            "Virtual account created.",
            data=VirtualAccountSerializer(va, context={"request": request}).data, status=201,
        )


class VirtualAccountDetailView(APIView):
    """GET one virtual account, or PATCH its status (activate / deactivate).

    docstring-name: Virtual accounts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    def rbac_permission(self):
        return (  # Use manage permission for PATCH, view permission otherwise.
            "payments.virtual_account.manage"
            if self.request.method == "PATCH"
            else "payments.virtual_account.view"
        )

    def _get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        va = (VirtualAccount.objects  # Look up the virtual account within the tenant.
              .filter(entity=entity, pk=pk)
              .select_related("customer", "deposit_account", "currency").first())
        if va is None:  # Return 404 when the record doesn't belong to this entity.
            raise NotFound("No virtual account matches this id for the entity.")
        return entity, va  # Return the resolved pair for reuse by GET/PATCH.

    def get(self, request, pk):
        _, va = self._get(request, pk)  # Reuse the shared entity-scoped lookup.
        return success_response(  # Return the serialized account.
            "Virtual account retrieved.",
            data=VirtualAccountSerializer(va, context={"request": request}).data)

    def patch(self, request, pk):
        _, va = self._get(request, pk)  # Fetch the account first.
        status_ = str(request.data.get("status", "")).upper()  # Normalize the requested status.
        services.set_virtual_account_status(va, status=status_, actor_user=request.user)  # Delegate the lifecycle change.
        return success_response(  # Return the updated account after the service call.
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


class PayoutListCreateView(APIView):
    """GET (list) / POST (initiate) payouts for an entity.

    docstring-name: Payouts
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    def rbac_permission(self):
        return (  # POST needs create permission; GET needs view permission.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutInstruction.objects.filter(entity=entity)  # Start from all payout instructions.
        if (group := request.query_params.get("group")) in PAYOUT_GROUPS:  # Support grouped UI filters.
            qs = qs.filter(status__in=PAYOUT_GROUPS[group])  # Expand the group into underlying statuses.
        elif (status_ := request.query_params.get("status")):  # Allow direct status filtering too.
            qs = qs.filter(status=status_)  # Filter by exact status.
        if (provider := request.query_params.get("provider")):  # Optional provider filter.
            qs = qs.filter(provider=provider)  # Narrow to one PSP.
        return _paginate(request, qs.order_by("-created_at", "-id"), PayoutInstructionSerializer, self)  # Return paginated rows.

    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        body = request.data  # Read the incoming payload once.
        amount = int(body.get("amount") or 0)  # Normalize amount to integer kobo.
        if amount <= 0:  # Reject invalid payout amounts.
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        for field in ("beneficiary_name", "beneficiary_account_number"):  # Validate the required beneficiary fields.
            if not body.get(field):  # Missing beneficiary data is a hard error.
                raise ValidationError({field: "This field is required."})
        # A payout settles a vendor's payable, so a vendor is required (it is what
        # confirmation books — Dr the vendor's AP control / Cr bank).  # Vendor is needed for AP posting.
        if not body.get("vendor"):  # Require a vendor for every payout.
            raise ValidationError({"vendor": "A payout must be linked to a vendor."})
        from django.db.models import Q  # Build flexible vendor lookup expressions.
        from vs_procurement.models import Vendor  # Procurement vendor model.
        raw = str(body.get("vendor"))  # Normalize the submitted vendor reference.
        lookup = Q(code=raw) | Q(pk=raw) if raw.isdigit() else Q(code=raw)  # Allow code or numeric id lookup.
        vendor = Vendor.objects.filter(entity=entity).filter(lookup).first()  # Resolve the vendor within the entity.
        if vendor is None:  # Reject unknown vendors.
            raise ValidationError({"vendor": "No such vendor in this entity."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")  # Optional source account.
        payout = services.initiate_payout(  # Delegate to the payment service layer.
            entity=entity, amount=amount, beneficiary_name=body["beneficiary_name"],
            beneficiary_account_number=body["beneficiary_account_number"],
            beneficiary_bank_code=body.get("beneficiary_bank_code", ""), vendor=vendor,
            source_account=source, provider=body.get("provider"),
            narration=body.get("narration", ""), wht_amount=int(body.get("wht_amount") or 0),
            metadata=body.get("metadata") or {}, actor_user=request.user,
        )
        return success_response(  # Return the created payout in the standard envelope.
            "Payout initiated.", data=PayoutInstructionSerializer(payout).data, status=201,
        )


class PayoutSummaryView(APIView):
    """GET /payments/payouts/summary/ — KPI totals + status-group counts over ALL rows.
    Honors ?provider.

    docstring-name: Payouts summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.payout.view"  # Payout summary is view-only.

    def get(self, request):
        import datetime  # Needed for the 7-day cutoff.

        from django.db.models import Count, Q, Sum  # Aggregate helper functions.
        from django.db.models.functions import Coalesce  # Replace NULL sums with zero.

        entity = resolve_entity(request)  # Scope the summary to the current entity.
        qs = PayoutInstruction.objects.filter(entity=entity)  # Start from all payout instructions.
        if (provider := request.query_params.get("provider")):  # Optional provider filter.
            qs = qs.filter(provider=provider)  # Narrow the aggregation to one PSP.
        cutoff = timezone.now() - datetime.timedelta(days=7)  # Define the recent 7-day window.
        agg = qs.aggregate(  # Compute the payout KPIs in one query.
            total=Count("id"),  # Total payout rows.
            settled7d=Coalesce(Sum("amount", filter=Q(status="PAID", confirmed_at__gte=cutoff)), 0),  # Settled in the last 7 days.
            pending=Coalesce(Sum("amount", filter=Q(status__in=PAYOUT_GROUPS["PENDING"])), 0),  # Pending amount.
            paid_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["PAID"])),  # Count of paid rows.
            pending_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["PENDING"])),  # Count of pending rows.
            failed_c=Count("id", filter=Q(status__in=PAYOUT_GROUPS["FAILED"])),  # Count of failed rows.
        )
        return success_response("Payouts summary retrieved.", data={  # Return the KPI payload.
            "total": agg["total"],
            "settled7d": {"kobo": agg["settled7d"], "naira": format_naira(agg["settled7d"])},
            "pending": {"kobo": agg["pending"], "naira": format_naira(agg["pending"])},
            "failed": agg["failed_c"],
            "group_counts": {"PAID": agg["paid_c"], "PENDING": agg["pending_c"], "FAILED": agg["failed_c"]},
        })


class PayoutBatchListCreateView(APIView):
    """GET (list) / POST (assemble a bulk batch of payouts) for an entity.

    POST creates the batch and its child instructions in ``DRAFT`` — it does **not**
    submit. Pass ``{"submit": true}`` to dispatch immediately after assembly.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    def rbac_permission(self):
        return (  # POST needs create permission; GET needs view permission.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutBatch.objects.filter(entity=entity)  # Start from all batches in the entity.
        if (status_ := request.query_params.get("status")):  # Optional status filter.
            qs = qs.filter(status=status_)  # Narrow by batch status.
        return _paginate(request, qs.order_by("-created_at", "-id"), PayoutBatchSummarySerializer, self)  # Return paginated summaries.

    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        body = request.data  # Read the incoming batch payload.
        raw_items = body.get("items")  # Extract the raw item list.
        if not isinstance(raw_items, list) or not raw_items:  # Require at least one item.
            raise ValidationError({"items": "A non-empty list of payout items is required."})
        source = _entity_obj(entity, Account, body.get("source_account"), "source_account")  # Optional batch source account.
        items = []  # Build the normalized batch items here.
        for idx, raw in enumerate(raw_items):  # Normalize each submitted line item.
            amount = int(raw.get("amount") or 0)  # Normalize amount to integer kobo.
            if amount <= 0:  # Reject empty or negative line amounts.
                raise ValidationError({f"items[{idx}].amount": "A positive amount (kobo) is required."})
            for field in ("beneficiary_name", "beneficiary_account_number"):  # Validate beneficiary fields per line.
                if not raw.get(field):  # Missing required data is a line-level error.
                    raise ValidationError({f"items[{idx}].{field}": "This field is required."})
            # Each line settles a vendor's payable on confirmation, so a vendor is
            # required (resolved by code or id — the picker emits codes).  # Vendor drives the AP posting later.
            if not raw.get("vendor"):  # Every line must map to a vendor.
                raise ValidationError({f"items[{idx}].vendor": "Each line must be linked to a vendor."})
            from django.db.models import Q  # Build the vendor lookup expression.
            from vs_procurement.models import Vendor  # Procurement vendor model.
            vref = str(raw.get("vendor"))  # Normalize the vendor reference.
            vlookup = Q(code=vref) | Q(pk=vref) if vref.isdigit() else Q(code=vref)  # Support code or numeric id.
            vendor = Vendor.objects.filter(entity=entity).filter(vlookup).first()  # Resolve the vendor within the entity.
            if vendor is None:  # Reject unknown vendors.
                raise ValidationError({f"items[{idx}].vendor": "No such vendor in this entity."})
            items.append({
                "amount": amount,  # Normalized line amount.
                "beneficiary_name": raw["beneficiary_name"],  # Beneficiary display name.
                "beneficiary_account_number": raw["beneficiary_account_number"],  # Beneficiary account number.
                "beneficiary_bank_code": raw.get("beneficiary_bank_code", ""),  # Optional bank code.
                "vendor": vendor,  # Resolved vendor object.
                "narration": raw.get("narration", ""),  # Optional line narration.
                "wht_amount": int(raw.get("wht_amount") or 0),  # Optional WHT amount.
                "metadata": raw.get("metadata") or {},  # Preserve caller metadata.
            })  # Keep the normalized payout item.
        batch = services.create_payout_batch(  # Assemble the draft batch in the service layer.
            entity=entity, items=items, provider=body.get("provider"),
            source_account=source, title=body.get("title", ""),
            narration=body.get("narration", ""), actor_user=request.user,
        )
        if body.get("submit") in (True, "1", "true", "True"):  # Optional immediate submission flag.
            batch = services.submit_payout_batch(batch, actor_user=request.user)  # Submit the draft batch immediately.
        return success_response(  # Return the batch in the standard envelope.
            "Payout batch created.", data=PayoutBatchSerializer(batch).data, status=201,
        )


class PayoutBatchSummaryView(APIView):
    """GET /payments/payout-batches/summary/ — batch KPI totals over ALL rows.

    docstring-name: Payout batches summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.payout.view"  # View permission is enough for batch summaries.

    def get(self, request):
        import datetime  # Needed for the 7-day completion window.

        from django.db.models import Count, Q, Sum  # Aggregate helpers.
        from django.db.models.functions import Coalesce  # Replace NULLs with zero.

        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PayoutBatch.objects.filter(entity=entity)  # Start from all batches in the entity.
        cutoff = timezone.now() - datetime.timedelta(days=7)  # Define the recent completion window.
        agg = qs.aggregate(  # Compute the batch summary metrics.
            total=Count("id"),  # Total batch count.
            queued=Coalesce(Sum("total_amount", filter=Q(status__in=["DRAFT", "PROCESSING"])), 0),  # Amount queued in draft/processing.
            completed7d=Count("id", filter=Q(status="COMPLETED", submitted_at__gte=cutoff)),  # Completed in the last 7 days.
            drafts=Count("id", filter=Q(status="DRAFT")),  # Draft batch count.
        )
        return success_response("Payout batches summary retrieved.", data={  # Return the batch KPI payload.
            "total": agg["total"],
            "queued": {"kobo": agg["queued"], "naira": format_naira(agg["queued"])},
            "completed7d": agg["completed7d"],
            "drafts": agg["drafts"],
        })


class PayoutBatchDetailView(APIView):
    """GET a batch with its items; POST submits the batch's pending instructions.

    docstring-name: Payout batches
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.

    @property
    def rbac_permission(self):
        return (  # POST submits a batch; GET only views it.
            "payments.payout.create"
            if self.request.method == "POST"
            else "payments.payout.view"
        )

    def get(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()  # Look up the batch within the tenant.
        if batch is None:  # Return 404 when the batch does not belong to this tenant.
            raise NotFound("No such payout batch in this entity.")
        return success_response(  # Return the serialized batch details.
            "Payout batch retrieved.", data=PayoutBatchSerializer(batch).data,
        )

    def post(self, request, pk):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        batch = PayoutBatch.objects.filter(entity=entity, pk=pk).first()  # Look up the batch within the tenant.
        if batch is None:  # Return 404 when the batch does not belong to this tenant.
            raise NotFound("No such payout batch in this entity.")
        batch = services.submit_payout_batch(batch, actor_user=request.user)  # Submit pending instructions.
        return success_response(  # Return the updated batch after submission.
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

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    def get(self, request):
        import datetime  # Needed for ISO date parsing.

        entity = resolve_entity(request)  # Resolve the tenant entity.

        def _date(name):
            raw = request.query_params.get(name)  # Read the query-string date.
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
        return success_response("Settlement reconciliation retrieved.", data=data)  # Return the reconciliation data.


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

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        qs = PaymentEvent.objects.filter(entity=entity).select_related("actor_user")  # Query the immutable audit log.
        if (action := request.query_params.get("action")):  # Optional action filter.
            qs = qs.filter(action=action)  # Narrow to a single gateway action.
        if (provider := request.query_params.get("provider")):  # Optional provider filter.
            qs = qs.filter(provider=provider)  # Narrow to one PSP.
        succeeded = request.query_params.get("succeeded")  # Optional success filter.
        if succeeded in ("true", "True", "1"):  # Explicitly request successful events.
            qs = qs.filter(succeeded=True)  # Return only successes.
        elif succeeded in ("false", "False", "0"):  # Explicitly request failed events.
            qs = qs.filter(succeeded=False)  # Return only failures.
        return _paginate(request, qs.order_by("-created_at", "-id"), PaymentEventSerializer, self)  # Return paginated log rows.


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


def _movement_querysets(entity, *, provider=None, group=None):
    """The collection (in) + payout (out) value-querysets projected to a common shape."""
    from django.db.models import CharField, F, Value  # Used to annotate common projection columns.
    from django.db.models.functions import Coalesce  # Used to prefer customer name over payer name.

    cols = CollectionIntent.objects.filter(entity=entity)  # Incoming movement source queryset.
    pos = PayoutInstruction.objects.filter(entity=entity)  # Outgoing movement source queryset.
    if provider:  # Optional PSP filter applied to both sides.
        cols = cols.filter(provider=provider)  # Filter collections by provider.
        pos = pos.filter(provider=provider)  # Filter payouts by provider.
    if group in MOVEMENT_GROUPS:  # Optional status-group filter.
        c_st, p_st = MOVEMENT_GROUPS[group]  # Split the collection and payout status sets.
        cols = cols.filter(status__in=c_st) if c_st else cols.none()  # Hide collections not in the requested group.
        pos = pos.filter(status__in=p_st) if p_st else pos.none()  # Hide payouts not in the requested group.

    cv = cols.annotate(  # Project collections into the common movement shape.
        kind=Value("collection", output_field=CharField()), gateway_id=F("id"),
        direction=Value("in", output_field=CharField()),
        party=Coalesce(F("customer__name"), F("payer_name"), Value(""), output_field=CharField()),
        linked_id=F("payment_id"), email=F("payer_email"),
        account_code=F("deposit_account__code"), account_name=F("deposit_account__name"),
        beneficiary_account=Value("", output_field=CharField()),
    ).values(*_MOVEMENT_COLS)
    pv = pos.annotate(  # Project payouts into the same shape.
        kind=Value("payout", output_field=CharField()), gateway_id=F("id"),
        direction=Value("out", output_field=CharField()), party=F("beneficiary_name"),
        linked_id=F("vendor_payment_id"), email=Value("", output_field=CharField()),
        account_code=F("source_account__code"), account_name=F("source_account__name"),
        beneficiary_account=F("beneficiary_account_number"),
    ).values(*_MOVEMENT_COLS)
    return cv, pv  # Return both common-shape querysets for the feed.


class MovementsView(APIView):
    """GET /payments/movements/ — unified, paginated money-movement feed: confirmed-or-
    pending collections (in) + payouts (out), newest first. Filters: ``?direction=in|out``,
    ``?group=SETTLED|PENDING|FAILED|REFUNDED``, ``?provider=``. Payout beneficiary
    name/account are FLS-masked without payments.payout.view_sensitive.

    docstring-name: Movements feed
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    def get(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        provider = request.query_params.get("provider")  # Optional provider filter.
        group = request.query_params.get("group")  # Optional status-group filter.
        direction = request.query_params.get("direction")  # Optional in/out filter.
        cv, pv = _movement_querysets(entity, provider=provider, group=group)  # Build the projected querysets.

        parts = []  # Collect whichever sides the caller requested.
        if direction != "out":  # Include collections unless the caller asked for payouts only.
            parts.append(cv)  # Add the collection queryset.
        if direction != "in":  # Include payouts unless the caller asked for collections only.
            parts.append(pv)  # Add the payout queryset.
        union = parts[0] if len(parts) == 1 else parts[0].union(parts[1], all=True)  # Union both sides when needed.
        union = union.order_by("-created_at")  # Show newest movements first.

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


class MovementsSummaryView(APIView):
    """GET /payments/movements/summary/ — money-in (7d) / money-out (7d) / pending / failed
    across both gateways, for the Transactions Log header.

    docstring-name: Movements summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    def get(self, request):
        import datetime  # Needed for the 7-day summary window.

        from django.db.models import Count, Q, Sum  # Aggregate helpers.
        from django.db.models.functions import Coalesce  # Replace NULL sums with zero.

        entity = resolve_entity(request)  # Resolve the tenant entity.
        provider = request.query_params.get("provider")  # Optional PSP filter.
        cols = CollectionIntent.objects.filter(entity=entity)  # Incoming movement queryset.
        pos = PayoutInstruction.objects.filter(entity=entity)  # Outgoing movement queryset.
        if provider:  # Apply the provider filter to both sides when requested.
            cols = cols.filter(provider=provider)  # Narrow collections.
            pos = pos.filter(provider=provider)  # Narrow payouts.
        cutoff = timezone.now() - datetime.timedelta(days=7)  # Define the recent-window cutoff.
        c = cols.aggregate(  # Compute collection-side KPIs.
            in7d=Coalesce(Sum("amount", filter=Q(status="SUCCEEDED", confirmed_at__gte=cutoff)), 0),  # Collections settled in 7 days.
            pending=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["PENDING"][0])),  # Pending collection count.
            failed=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["FAILED"][0])),  # Failed collection count.
        )
        p = pos.aggregate(  # Compute payout-side KPIs.
            out7d=Coalesce(Sum("amount", filter=Q(status="PAID", confirmed_at__gte=cutoff)), 0),  # Payouts settled in 7 days.
            pending=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["PENDING"][1])),  # Pending payout count.
            failed=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["FAILED"][1])),  # Failed payout count.
        )
        return success_response("Movements summary retrieved.", data={  # Return the combined KPI payload.
            "in7d": {"kobo": c["in7d"], "naira": format_naira(c["in7d"])},
            "out7d": {"kobo": p["out7d"], "naira": format_naira(p["out7d"])},
            "pending": c["pending"] + p["pending"],
            "failed": c["failed"] + p["failed"],
        })


# --------------------------------------------------------------------------- #
# Webhook receiver (public, signature-verified)                               #
# --------------------------------------------------------------------------- #

class WebhookView(APIView):
    """POST /webhooks/<provider>/ — raw signed PSP event. No JWT; signature is the auth.

    docstring-name: PSP webhook receiver
    """

    authentication_classes: list = []  # Webhooks authenticate by signature, not session/JWT.
    permission_classes = [AllowAny]  # Public endpoint for PSP callbacks.

    def post(self, request, provider):
        try:  # Duplicate events are expected and should be acknowledged.
            event = webhooks.ingest_webhook(  # Hand the raw signed request to the webhook ingestion layer.
                provider=provider, raw_body=request.body, headers=dict(request.headers),
            )
        except DuplicateWebhookError:  # Already processed; acknowledge so the provider stops retrying.
            # Already handled — acknowledge so the provider stops retrying.
            return success_response("Duplicate event ignored.", data={"duplicate": True})
        return success_response(  # Return the processed webhook id and status.
            "Webhook processed.", data={"id": event.id, "status": event.status},
        )
