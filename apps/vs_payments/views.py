"""REST API for vs_payments.  # Thin read/write API layer for the gateway app.

Two kinds of endpoint:

* **Authenticated, entity-scoped** actions/reads (``?entity=<id|code>``) for initiating
  collections/payouts, provisioning virtual accounts and listing gateway records. These
  use the platform envelope + RBAC (``payments.<resource>.<action>``), exactly like
  ``vs_finance``.  # All tenant-scoped writes and reads go through RBAC.
* A **public webhook receiver** (``/webhooks/<provider>/``) that takes the raw signed
  body from the PSP. It is ``AllowAny`` because the PSP can't carry a JWT - authenticity
  comes from the body signature, verified inside :func:`vs_payments.webhooks.ingest_webhook`.  # Webhooks authenticate by signature, not JWT.

Domain errors raised by the services/webhooks render through the shared typed-exception
handler, so the views stay thin.  # Keep business logic in services, not views.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.models import Account, Customer, Invoice
from vs_finance.views import resolve_entity
from vs_finance.views_ops.base import _resolve_account
from vs_rbac.field_enforcement import can_read
from vs_rbac.permissions import (
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
)
from vs_rbac.scoping import transaction_branch_q

from . import reconciliation, services, webhooks
from .reach import PaymentsReach
from .constants import (
    COLLECTION_GROUPS,
    PAYOUT_GROUPS,
    VirtualAccountStatus,
    WebhookStatus,
)
from .exceptions import DuplicateWebhookError, PayoutApprovalRequiredError
from .providers.registry import is_available as provider_is_available
from .models import WebhookEvent
from .serializers import (
    CollectionIntentSerializer,
    PaymentEventSerializer,
    PayoutBatchSerializer,
    PayoutBatchSummarySerializer,
    PayoutInstructionSerializer,
    VirtualAccountSerializer,
    WebhookEventSerializer,
)


# Support the paginate workflow.
def _paginate(request, qs, serializer_cls, view, **ser_kwargs):
    """Paginate a queryset through the platform's XVSPagination envelope ({pagination, data}).
    Page size is a fixed 25 (override per-request with ?page_size=, capped at 100)."""
    paginator = XVSPagination()  # Build the shared pagination helper.
    paginator.page_size = 25  # Default to 25 rows per page for this API.
    page = paginator.paginate_queryset(qs, request, view=view)  # Slice the queryset for the current page.
    ser_kwargs.setdefault("context", {"request": request})
    return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Wrap the serialized page.


def _reach(request):
    """The entity this request names and the gateway records in it the caller may see.

    Every payments read and status change starts here rather than from a model
    manager; see :mod:`vs_payments.reach` for the rule and why.
    """
    entity = resolve_entity(request)
    return entity, PaymentsReach.for_request(request, entity)


# Support the entity obj workflow.
def _entity_obj(request, entity, model, ref, field):
    """Fetch ``model`` within ``entity`` and the caller's branches by pk or ``code``.

    Codes resolve too, because the UI pickers emit them. A row with a branch is
    found only when it is one of the caller's own branches', read exclusively as
    :class:`~vs_payments.reach.PaymentsReach` reads the gateway record created
    against it: a clerk never creates a record they then cannot open. Ikeja's
    clerk naming a Lekki customer or invoice, or one not yet given a branch, gets
    the same 400 as for one that does not exist."""
    if model is Account:
        # A ledger account is named through finance's resolver, which also applies
        # the caller's bank reach; this lookup does not know the caller.
        raise TypeError("Resolve a ledger account with vs_finance.views_ops.base._resolve_account.")
    if ref in (None, ""):  # Blank inputs are allowed to resolve to nothing.
        return None
    qs = model.objects.filter(entity=entity)
    if any(getattr(f, "name", None) == "branch" for f in model._meta.get_fields()):
        qs = qs.filter(transaction_branch_q(request))
    has_code = any(getattr(f, "name", None) == "code" for f in model._meta.get_fields())  # Check whether the model exposes a code field.
    obj = None  # Hold the resolved object if any lookup succeeds.
    if str(ref).isdigit():  # Numeric refs might be pks or codes.
        obj = qs.filter(pk=ref).first()
    # Account (and other) codes are themselves numeric strings, so a digit ref may
    # be a *code*, not a pk - fall back to a code match before giving up.  # Handle numeric codes defensively.
    if obj is None and has_code:  # Only try a code lookup when the model supports one.
        obj = qs.filter(code__iexact=str(ref)).first()
    if obj is None:  # Nothing matched the entity-scoped lookup.
        raise ValidationError({field: f"No {model.__name__.lower()} '{ref}' in this entity."})
    return obj  # Return the resolved object.


def _collection_payer(request, entity, ref, invoice):
    """The customer a collection names, within reach or as the named invoice's owner.

    A collection for an invoice belongs to the invoice's branch
    (:func:`vs_payments.services.collection_branch_id`), so the family it bills
    need not be in the caller's reach. The Okafor family is filed under Ikeja and
    owes a Lekki invoice: Tola, who keeps Lekki's books, may name them beside
    that invoice. Any other family outside her branches, including one that does
    not own the invoice, is refused exactly as one that does not exist.
    """
    try:
        return _entity_obj(request, entity, Customer, ref, "customer")
    except ValidationError:
        if invoice is None:
            raise
        owner = Customer.objects.filter(entity=entity, pk=invoice.customer_id)
        match = Q(code__iexact=str(ref))
        if str(ref).isdigit():
            match |= Q(pk=int(ref))
        payer = owner.filter(match).first()
        if payer is None:
            raise
        return payer


def _collection_deposit(request, entity, ref, *, customer, invoice=None, noun):
    """The ledger account money collected for ``customer`` lands in, checked for its branch.

    Money is deposited into the branch its receipt is booked to
    (:func:`vs_payments.services.collection_branch_id`). Where the invoice, the
    customer or a one-branch school decides that branch, the account must be that
    branch's. Where nothing does (a customer every branch shares, at a school with
    several, and no invoice), the account decides it: Mr Bello's top-up for the
    Adeyemi family, deposited into Lekki's collection account, is Lekki's money.
    Such a collection must therefore name an account that is a branch's, or its
    receipt would be booked to no branch at all. Books with no branch (the
    platform's) have none to name and are not asked.
    """
    from vs_tenants.models import Branch

    branch_id = services.collection_branch_id(customer=customer, invoice=invoice)
    has_branches = Branch.all_objects.filter(tenant_id=entity.tenant_id).exists()
    if branch_id is not None or customer is None or not has_branches:
        return _resolve_account(
            request, entity, ref, "deposit_account",
            document_branch=branch_id, noun=noun, verb="Deposit it into")
    deposit = _resolve_account(request, entity, ref, "deposit_account")
    if services.deposit_branch_id(deposit) is None:
        raise ValidationError({"deposit_account": (
            f"{customer.name} is shared by every branch, so the account this is "
            f"deposited into decides whose money it is. Name a branch's bank account."
        )})
    return deposit


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _required_idempotency_key(request) -> str:
    """Validate the standard creation key without silently rewriting it."""
    value = request.headers.get("Idempotency-Key")
    if value in (None, ""):
        raise ValidationError({"idempotency_key": "The Idempotency-Key header is required."})
    key = str(value)
    if key != key.strip():
        raise ValidationError({"idempotency_key": "Leading or trailing whitespace is not allowed."})
    if len(key) > 128:
        raise ValidationError({"idempotency_key": "Must be 128 characters or fewer."})
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValidationError({
            "idempotency_key": "Use only letters, numbers, dot, underscore, colon, or hyphen.",
        })
    return key


def _payout_vendor(request, entity, reference):
    """Resolve a vendor by id or code inside the selected entity and the caller's branches.

    Read exclusively, as :class:`~vs_payments.reach.PaymentsReach` reads the payout
    through its vendor: a vendor of another branch, or one every branch shares,
    answers like one that does not exist, so a clerk never raises a payout they
    then cannot open.
    """
    from django.db.models import Q
    from vs_procurement.models import Vendor

    raw = str(reference or "")
    if not raw:
        raise ValidationError({"vendor": "A payout must be linked to a vendor."})
    lookup = Q(code=raw) | Q(pk=raw) if raw.isdigit() else Q(code=raw)
    vendor = (
        Vendor.objects.filter(entity=entity)
        .filter(transaction_branch_q(request)).filter(lookup).first()
    )
    if vendor is None:
        raise ValidationError({"vendor": "No such vendor in this entity."})
    return vendor


def _legacy_beneficiary_fields(body) -> dict:
    """Carry compatibility fields only when the caller actually supplied them."""
    return {
        field: body.get(field)
        for field in (
            "beneficiary_name", "beneficiary_account_number", "beneficiary_bank_code",
        )
        if field in body
    }


# --------------------------------------------------------------------------- #
# Collections                                                                 #
# --------------------------------------------------------------------------- #

# Console status groups -> underlying CollectionStatus values, defined in
# constants.py and re-exported here. The Export Centre's screen bindings
# need the same expansion to make a quick export match its table.


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
        _, reach = _reach(request)
        qs = reach.collections().select_related("entity", "customer", "deposit_account", "payment")
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
        
        invoice = _entity_obj(request, entity, Invoice, body.get("invoice"), "invoice")
        customer = _collection_payer(request, entity, body.get("customer"), invoice)
        deposit = _collection_deposit(
            request, entity, body.get("deposit_account"), customer=customer, invoice=invoice,
            noun="payment request")

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
    """GET /payments/collections/summary/ - KPI totals (kobo) + status-group counts over
    ALL rows, so the header stays accurate while the list paginates. Honors ?provider.

    docstring-name: Collections summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.collection.view"  # Collections summary uses view permission only.

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        _, reach = _reach(request)
        qs = reach.collections()
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
        _, reach = _reach(request)
        intent = reach.get_or_404(reach.collections(), pk, "No such collection in this entity.")

        if request.query_params.get("verify") in ("1", "true", "True"):
            intent = services.confirm_collection(intent, actor_user=request.user)  # Confirm against the provider before returning.
            
        return success_response("Collection retrieved.", data=CollectionIntentSerializer(intent).data)


# Group endpoint behavior for Virtual Account List Create View.
class VirtualAccountListCreateView(APIView):
    """GET (list) / POST (provision) dedicated virtual accounts for an entity.

    GET is paginated with filters (``status``, ``provider``, ``customer``,
    ``search``) and rides KPI counts (active / inactive / providers in use) in
    the envelope. The funding number and name are switched fields of
    ``payments.virtual_account``, so they are absent unless the caller's role
    reads them.

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
        _, reach = _reach(request)
        base = reach.virtual_accounts()
        kpis = {  # Compute the summary KPIs used by the list header.
            "total": base.count(),
            "active": base.filter(status=VirtualAccountStatus.ACTIVE).count(),
            "inactive": base.filter(status=VirtualAccountStatus.INACTIVE).count(),
            "providers": base.values("provider").distinct().count(),
        }
        qs = base.select_related("entity", "customer", "deposit_account", "currency")
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
        customer = _entity_obj(request, entity, Customer, request.data.get("customer"), "customer")
        if customer is None:  # Virtual accounts are always customer-specific in this flow.
            raise ValidationError({"customer": "A customer is required."})
        deposit = _collection_deposit(
            request, entity, request.data.get("deposit_account"), customer=customer,
            noun="virtual account")
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
        return (  # Use update permission for PATCH, view permission otherwise.
            "payments.virtual_account.update"
            if self.request.method == "PATCH"
            else "payments.virtual_account.view"
        )

    # Support the get workflow.
    def _get(self, request, pk):
        entity, reach = _reach(request)
        va = reach.get_or_404(
            reach.virtual_accounts().select_related("customer", "deposit_account", "currency"),
            pk, "No virtual account matches this id for the entity.")
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

# Console status groups → underlying PayoutStatus values (PAID shows as
# "Settled"). Defined in constants.py alongside COLLECTION_GROUPS.


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
        _, reach = _reach(request)
        qs = reach.payouts().select_related("entity", "source_account")
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
        idempotency_key = _required_idempotency_key(request)
        amount = int(body.get("amount") or 0)
        if amount <= 0:  # Reject invalid payout amounts.
            raise ValidationError({"amount": "A positive amount (in kobo) is required."})
        vendor = _payout_vendor(request, entity, body.get("vendor"))
        source = _resolve_account(request, entity, body.get("source_account"), "source_account")
        item = {
            "amount": amount, "vendor": vendor,
            "narration": body.get("narration", ""),
            "wht_amount": body.get("wht_amount"),  # Omitted means computed from the vendor's code.
            "metadata": body.get("metadata") or {},
            **_legacy_beneficiary_fields(body),
        }
        with transaction.atomic():
            batch = services.create_payout_batch(
                entity=entity, items=[item], provider=body.get("provider"),
                source_account=source, title=body.get("title", ""),
                narration=body.get("narration", ""), actor_user=request.user,
                idempotency_key=idempotency_key, request_kind="single",
                submit_for_approval=True,
            )
            replay = bool(getattr(batch, "_idempotency_replay", False))
            instance = services.submit_payout_batch_for_approval(
                batch, requested_by=request.user,
                confirm_without_approval=bool((request.data or {}).get("confirm_without_approval")),
                confirmation_reason=str((request.data or {}).get("reason") or "").strip(),
            )
            payout = batch.instructions.get()
        from vs_workflow.services import release as release_svc

        return success_response(
            "Payout already queued for approval." if replay else "Payout queued for approval.",
            data=PayoutInstructionSerializer(
                payout, context={"request": request},
            ).data | {"approval": release_svc.approval_block(instance)},
            status=200 if replay else 201,
        )


# Group endpoint behavior for Payout Summary View.
class PayoutSummaryView(APIView):
    """GET /payments/payouts/summary/ - KPI totals + status-group counts over ALL rows.
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

        _, reach = _reach(request)
        qs = reach.payouts()
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

    POST creates the batch and its child instructions in ``DRAFT``. Pass
    ``{"submit": true}`` to submit the batch for approval. Neither path calls the
    provider directly.

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
        _, reach = _reach(request)
        qs = reach.batches().select_related("entity")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return _paginate(request, qs.order_by("-created_at", "-id"), PayoutBatchSummarySerializer, self)

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)  # Resolve the tenant entity.
        body = request.data  # Read the incoming batch payload.
        idempotency_key = _required_idempotency_key(request)
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:  # Require at least one item.
            raise ValidationError({"items": "A non-empty list of payout items is required."})
        source = _resolve_account(request, entity, body.get("source_account"), "source_account")
        items = []  # Build the normalized batch items here.
        for idx, raw in enumerate(raw_items):  # Normalize each submitted line item.
            amount = int(raw.get("amount") or 0)
            if amount <= 0:  # Reject empty or negative line amounts.
                raise ValidationError({f"items[{idx}].amount": "A positive amount (kobo) is required."})
            try:
                vendor = _payout_vendor(request, entity, raw.get("vendor"))
            except ValidationError as exc:
                raise ValidationError({f"items[{idx}].vendor": "No such vendor in this entity."}) from exc
            items.append({
                "amount": amount,  # Normalized line amount.
                "vendor": vendor,  # Resolved vendor object.
                "narration": raw.get("narration", ""),
                "wht_amount": raw.get("wht_amount"),  # Omitted means computed from the vendor's code.
                "metadata": raw.get("metadata") or {},
                **_legacy_beneficiary_fields(raw),
            })  # Keep the normalized payout item.
        wants_submit = body.get("submit") in (True, "1", "true", "True")
        with transaction.atomic():
            batch = services.create_payout_batch(  # Assemble the draft batch in the service layer.
                entity=entity, items=items, provider=body.get("provider"),
                source_account=source, title=body.get("title", ""),
                narration=body.get("narration", ""), actor_user=request.user,
                idempotency_key=idempotency_key, request_kind="batch",
                submit_for_approval=wants_submit,
            )
            replay = bool(getattr(batch, "_idempotency_replay", False))
            instance = None
            if wants_submit:
                instance = services.submit_payout_batch_for_approval(
                    batch, requested_by=request.user,
                confirm_without_approval=bool((request.data or {}).get("confirm_without_approval")),
                confirmation_reason=str((request.data or {}).get("reason") or "").strip(),
            )
        data = PayoutBatchSerializer(batch, context={"request": request}).data
        if instance is not None:
            from vs_workflow.services import release as release_svc
            data |= {"approval": release_svc.approval_block(instance)}
        return success_response(
            "Payout batch already exists." if replay else "Payout batch created.",
            data=data, status=200 if replay else 201,
        )


# Group endpoint behavior for Payout Batch Summary View.
class PayoutBatchSummaryView(APIView):
    """GET /payments/payout-batches/summary/ - batch KPI totals over ALL rows.

    docstring-name: Payout batches summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.payout.view"  # View permission is enough for batch summaries.

    # Handle GET requests for this endpoint.
    def get(self, request):
        import datetime

        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        from .constants import PayoutStatus  # In-flight statuses backing the queued-money KPI.

        _, reach = _reach(request)
        qs = reach.batches()
        cutoff = timezone.now() - datetime.timedelta(days=7)
        agg = qs.aggregate(
            total=Count("id"),
            completed7d=Count("id", filter=Q(status="COMPLETED", submitted_at__gte=cutoff)),
            drafts=Count("id", filter=Q(status="DRAFT")),
        )
        # "queued" money must reflect only genuinely in-flight child instructions, not the
        # batch total - a PROCESSING batch can carry FAILED children that never left.  # Sum child amounts, not batch totals.
        queued_kobo = reach.payouts().filter(
            batch__isnull=False,
            status__in=[PayoutStatus.PENDING, PayoutStatus.PROCESSING],
        ).aggregate(s=Coalesce(Sum("amount"), 0))["s"]
        return success_response("Payout batches summary retrieved.", data={
            "total": agg["total"],
            "queued": {"kobo": queued_kobo, "naira": format_naira(queued_kobo)},
            "completed7d": agg["completed7d"],
            "drafts": agg["drafts"],
        })


# Group endpoint behavior for Payout Batch Detail View.
class PayoutBatchDetailView(APIView):
    """GET a batch with its items; POST refuses direct provider submission.

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
        _, reach = _reach(request)
        batch = reach.get_or_404(reach.batches(), pk, "No such payout batch in this entity.")
        return success_response(
            "Payout batch retrieved.",
            data=PayoutBatchSerializer(batch, context={"request": request}).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _, reach = _reach(request)
        batch = reach.get_or_404(reach.batches(), pk, "No such payout batch in this entity.")
        raise PayoutApprovalRequiredError(
            "Direct payout batch submission is disabled. Submit the batch for approval.",
        )


# Group endpoint behavior for Payout Batch Submit-For-Approval View.
class PayoutBatchSubmitForApprovalView(APIView):
    """POST /payments/payout-batches/<id>/submit-for-approval/ - route a batch through approval.

    Hands the batch to the vs_workflow engine; the handler's ``validate_document``
    runs the submit preflight (draft batch with pending instructions) and records the
    batch as awaiting approval. The provider submission fires only on final approval.

    docstring-name: Submit a payout batch for approval
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Tenant-authenticated access only.
    rbac_permission = "payments.payout_batch.submit"  # Distinct submit-for-approval key.

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _, reach = _reach(request)
        batch = reach.get_or_404(reach.batches(), pk, "No such payout batch in this entity.")
        from vs_workflow.services import release as release_svc

        instance = services.submit_payout_batch_for_approval(
            batch, requested_by=request.user,
                confirm_without_approval=bool((request.data or {}).get("confirm_without_approval")),
                confirmation_reason=str((request.data or {}).get("reason") or "").strip(),
            )  # Instance + stage 1, replay-safe for this batch.
        batch.refresh_from_db()  # Pick up the handler's metadata change.
        return success_response(
            "Payout batch submitted for approval.",
            data=PayoutBatchSerializer(batch, context={"request": request}).data
            | {"approval": release_svc.approval_block(instance)},
        )


# --------------------------------------------------------------------------- #
# Settlement reconciliation (read-side report)                                #
# --------------------------------------------------------------------------- #

#: Which settlement view a ``?view=`` value renders, and the columns it carries.
_SETTLEMENT_VIEWS = ("matched", "unsettled", "unmatched")


# Render one settlement view as a downloadable file, or None when not asked for.
def _maybe_export_settlement(request, recon, entity):
    """``?export=csv|xlsx|pdf&view=matched|unsettled|unmatched`` on the settlement report.

    Reuses vs_finance's report renderer so a settlement file looks like every other
    finance export - this app already depends on vs_finance for money formatting and
    entity resolution, so no new coupling is introduced.

    The file is read by people, so its dates are the tenant's
    (:mod:`vs_config.display`): the window and a bank line's date in its date
    format, and a confirmation, which is an instant, on its wall clock.
    """
    if not request.query_params.get("export"):
        return None

    from vs_config.display import format_date, format_date_range, format_datetime
    from vs_finance.exports import ReportTable
    from vs_finance.views import _maybe_export

    view = (request.query_params.get("view") or "matched").lower()
    if view not in _SETTLEMENT_VIEWS:
        raise ValidationError({
            "view": f"Expected one of {', '.join(_SETTLEMENT_VIEWS)}.",
        })

    tenant = entity.tenant
    start, end = recon.start_date, recon.end_date
    if start and end:
        window = format_date_range(start, end, tenant)
    elif start:
        window = f"From {format_date(start, tenant)}"
    elif end:
        window = f"Up to {format_date(end, tenant)}"
    else:
        window = "All dates"
    subtitle = f"{recon.entity_code} · {window}"
    kind = {"COLLECTION": "Collection"}

    if view == "unmatched":
        table = ReportTable(
            title="Settlement - Unmatched bank lines",
            subtitle=subtitle,
            columns=["Date", "Description", "Reference", "Amount"],
            rows=[
                [format_date(b.txn_date, tenant), b.description, b.reference, b.amount_naira]
                for b in recon.unmatched_bank_lines
            ],
            summary_rows=[["", "TOTAL", "", format_naira(recon.unmatched_bank_total)]],
        )
    elif view == "unsettled":
        rows = [r for r in recon.rows if not r.settled]
        table = ReportTable(
            title="Settlement - Awaiting bank",
            subtitle=subtitle,
            columns=["Date", "Type", "Provider", "Reference", "Gross", "Status"],
            rows=[
                [
                    format_datetime(r.confirmed_at, tenant),
                    kind.get(r.kind, "Payout"), r.provider, r.reference,
                    r.amount_naira, "Awaiting bank",
                ]
                for r in rows
            ],
            summary_rows=[["", "TOTAL", "", "", format_naira(recon.unsettled_total), ""]],
        )
    else:
        rows = [r for r in recon.rows if r.settled]
        table = ReportTable(
            title="Settlement - Matched",
            subtitle=subtitle,
            columns=[
                "Date", "Type", "Provider", "Reference", "Gross", "Fees",
                "Net settled", "Settlement ref", "Match basis",
            ],
            rows=[
                [
                    format_datetime(r.confirmed_at, tenant),
                    kind.get(r.kind, "Payout"), r.provider, r.reference,
                    r.amount_naira,
                    format_naira(r.fee_amount or 0),
                    format_naira(abs(r.settled_amount if r.settled_amount is not None else r.amount)),
                    r.settlement_reference or "",
                    # The screen says "By amount" for an amount-only match, which is
                    # the one a reviewer must look at twice. The file says the same.
                    f"By {r.match_basis}" if r.match_basis else "",
                ]
                for r in rows
            ],
            summary_rows=[["", "TOTAL", "", "", "", "", format_naira(recon.settled_total), "", ""]],
        )

    return _maybe_export(request, table, filename=f"settlement_{view}_{entity.code}")


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

        entity, reach = _reach(request)

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
            provider=request.query_params.get("provider"), reach=reach,
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
                "needs_review_count": recon.needs_review_count,  # Amount-only matches to confirm.
            },
            "rows": [
                {
                    "kind": r.kind, "gateway_id": r.gateway_id, "reference": r.reference,
                    "provider": r.provider, "provider_reference": r.provider_reference,
                    "amount": r.amount, "amount_naira": r.amount_naira,
                    "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
                    "settled": r.settled, "match_basis": r.match_basis,
                    "needs_review": r.needs_review,  # Matched on amount alone → confirm.
                    "matched_bank_line_id": r.matched_bank_line_id,
                    "settled_amount": r.settled_amount, "fee_amount": r.fee_amount,
                    "settlement_reference": r.settlement_reference,
                    "settlement_date": r.settlement_date.isoformat() if r.settlement_date else None,
                    "settlement_description": r.settlement_description,
                    "via_clearing": r.via_clearing,  # Settles by a settlement match, not a line of its own.
                    "reported_fee": r.reported_fee,
                }
                for r in recon.rows  # Iterate through the relevant records.
            ],
            "suggested_settlements": recon.suggested_settlements,
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

        # ``?export=csv|xlsx|pdf`` renders one of the three views as a file, the
        # same way every finance report does. This report is a computed snapshot
        # rather than a queryset, so it has no Export Centre dataset and cannot
        # be a quick export - a server-rendered file is the equivalent.
        #
        # ``?view=`` follows the screen's tab, because the three tabs are three
        # different shapes: matched rows carry fees and a settlement reference,
        # unsettled rows have neither, and unmatched bank lines are not gateway
        # records at all. One file of all three would be three tables stacked
        # under one header.
        export = _maybe_export_settlement(request, recon, entity)
        if export is not None:
            return export

        return success_response("Settlement reconciliation retrieved.", data=data)


# Group endpoint behavior for Transactions Log View.
class TransactionsLogView(APIView):
    """GET the append-only gateway action log (the transactions log) for an entity.

    Reads :class:`~vs_payments.models.PaymentEvent` - the immutable record of every
    gateway action (collections, payouts, virtual accounts, webhooks) including failed
    and rejected attempts. Filterable by ``?action=``, ``?provider=`` and
    ``?succeeded=true|false``; paginated.

    docstring-name: Transactions log
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        _, reach = _reach(request)
        qs = reach.events().select_related("entity", "actor_user", "proxied_by")
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
# Movements - unified money-in (collections) + money-out (payouts) feed        #
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
    "amount", "status", "narration", "provider_reference", "confirmed_at",
    "email", "account_code", "account_name", "beneficiary_account",
    "sent_amount", "wht_amount",
]


# Support the movement querysets workflow.
def _movement_querysets(reach, *, provider=None, group=None):
    """The collection (in) + payout (out) value-querysets projected to a common shape.

    ``amount`` is each record's own figure, the gross of a payout line.
    ``sent_amount`` is what moved: a collection's amount, and a payout's line less
    its WHT (:func:`vs_payments.services.payout_sent_expression`), with
    ``wht_amount`` the difference. :class:`MovementsView` presents them.
    """
    from django.db.models import BigIntegerField, CharField, F, Value
    from django.db.models.functions import Coalesce

    cols = reach.collections()
    pos = reach.payouts()
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
        email=F("payer_email"),
        account_code=F("deposit_account__code"), account_name=F("deposit_account__name"),
        beneficiary_account=Value("", output_field=CharField()),
        sent_amount=F("amount"), wht_amount=Value(0, output_field=BigIntegerField()),
    ).values(*_MOVEMENT_COLS)
    pv = pos.annotate(
        kind=Value("payout", output_field=CharField()), gateway_id=F("id"),
        direction=Value("out", output_field=CharField()), party=F("beneficiary_name"),
        email=Value("", output_field=CharField()),
        account_code=F("source_account__code"), account_name=F("source_account__name"),
        beneficiary_account=F("beneficiary_account_number"),
        sent_amount=services.payout_sent_expression(),
        wht_amount=services.payout_wht_expression(),
    ).values(*_MOVEMENT_COLS)
    return cv, pv  # Return both common-shape querysets for the feed.


#: The feed's own column names for the two payout fields Field Access governs,
#: and the registry keys behind them.
#:
#: The feed puts a collection and a payout in one row shape, so its names are
#: neither side's: ``party`` is the customer on a collection and the
#: beneficiary on a payout, and no single switch could cover both. Registering
#: the feed's names as the payout field's own would therefore make one switch
#: mean two things, so the mapping lives here, at the one call site that needs
#: it, instead of in the registry.
_MOVEMENT_PAYOUT_FIELDS = {
    "party": "payments.payout.beneficiary_name",
    "beneficiary_account": "payments.payout.beneficiary_account_number",
}


# Group endpoint behavior for Movements View.
class MovementsView(APIView):
    """GET /payments/movements/ - unified, paginated money-movement feed: confirmed-or-
    pending collections (in) + payouts (out), newest first. Filters: ``?direction=in|out``,
    ``?group=SETTLED|PENDING|FAILED|REFUNDED``, ``?provider=``.

    ``amount`` is the money that moved: on a payout, the line less the WHT
    withheld, which is what was sent and what the bank shows. ``gross_amount``
    is the line itself and ``wht_amount`` the WHT, equal to ``amount`` and 0 on
    a collection.

    A payout row carries the beneficiary's name and account number only for a
    caller whose roles let them read those fields; for anybody else the keys
    are absent rather than masked, so the feed says no more about a payment
    than the payment's own record would.

    docstring-name: Movements feed
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]  # Read-only tenant access.
    rbac_permission = "payments.report.view"  # Reporting permission.

    # Handle GET requests for this endpoint.
    def get(self, request):
        _, reach = _reach(request)
        provider = request.query_params.get("provider")
        group = request.query_params.get("group")
        direction = request.query_params.get("direction")
        cv, pv = _movement_querysets(reach, provider=provider, group=group)  # Build the projected querysets.

        parts = []  # Collect whichever sides the caller requested.
        if direction != "out":  # Include collections unless the caller asked for payouts only.
            parts.append(cv)  # Add the collection queryset.
        if direction != "in":  # Include payouts unless the caller asked for collections only.
            parts.append(pv)  # Add the payout queryset.
        union = parts[0] if len(parts) == 1 else parts[0].union(parts[1], all=True)  # Union both sides when needed.
        union = union.order_by("-created_at")

        paginator = XVSPagination()  # Build the shared paginator.
        page = paginator.paginate_queryset(union, request, view=self)  # Slice the union query.
        # Evaluated once for the page; the map behind it is cached on the request.
        hidden = [
            name for name, key in _MOVEMENT_PAYOUT_FIELDS.items()
            if not can_read(request, key)
        ]
        rows = []  # Build the response rows explicitly so the beneficiary can be dropped.
        for m in page:  # Convert each result row into a serializable mapping.
            row = dict(m)  # Coerce the projected row into a plain dict.
            if row["kind"] == "payout":  # A collection's party is its customer, not a beneficiary.
                for name in hidden:  # Absent, not masked: a marker is itself an answer.
                    row.pop(name, None)
            row["gross_amount"] = row["amount"]  # The record's own figure.
            row["amount"] = row.pop("sent_amount")  # What moved, net of any WHT.
            row["amount_naira"] = format_naira(row["amount"])  # Add a display amount.
            row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None  # Normalize timestamps.
            row["confirmed_at"] = row["confirmed_at"].isoformat() if row["confirmed_at"] else None  # Normalize timestamps.
            rows.append(row)  # Accumulate the row.
        return paginator.get_paginated_response(rows)  # Return the paginated feed.


# Group endpoint behavior for Movements Summary View.
class MovementsSummaryView(APIView):
    """GET /payments/movements/summary/ - money-in (7d) / money-out (7d, net of WHT) / pending / failed
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

        _, reach = _reach(request)
        provider = request.query_params.get("provider")
        cols = reach.collections()
        pos = reach.payouts()
        if provider:  # Apply the provider filter to both sides when requested.
            cols = cols.filter(provider=provider)
            pos = pos.filter(provider=provider)
        cutoff = timezone.now() - datetime.timedelta(days=7)
        c = cols.aggregate(
            in7d=Coalesce(Sum("amount", filter=Q(status="SUCCEEDED", confirmed_at__gte=cutoff)), 0),
            pending=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["PENDING"][0])),
            failed=Count("id", filter=Q(status__in=MOVEMENT_GROUPS["FAILED"][0])),
        )
        p = pos.aggregate(  # Money out is what was sent, net of WHT.
            out7d=Coalesce(Sum(services.payout_sent_expression(),
                               filter=Q(status="PAID", confirmed_at__gte=cutoff)), 0),
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
    """POST /webhooks/<provider>/ - raw signed PSP event. No JWT; signature is the auth.

    A provider this deployment does not offer (an unknown name, or ``FAKE`` where the
    Fake provider is switched off) answers 404 before the body is read, exactly as if
    the route did not exist: nothing is verified, stored or audited, and the response
    does not reveal that the name is a provider somewhere else.

    The route is public, so it is throttled per client address under the
    ``payments_webhook`` scope. There is no user to key on (no authentication runs), so
    DRF keys the bucket by IP. The throttle runs before the handler, which means a
    refused request is answered 429 before its signature is checked: it is not stored
    and writes no audit row, so a flood of badly signed bodies cannot fill the audit
    log. The rate is set well above a provider's own retry bursts, which arrive from a
    small pool of addresses. The throttle class is named on the view rather than taken
    from ``DEFAULT_THROTTLE_CLASSES``, so this limit does not depend on the project
    default staying as it is.

    docstring-name: PSP webhook receiver
    """

    authentication_classes: list = []  # Webhooks authenticate by signature, not session/JWT.
    permission_classes = [AllowAny]  # Public endpoint for PSP callbacks.
    throttle_scope = "payments_webhook"
    throttle_classes = [ScopedRateThrottle]

    # Handle POST requests for this endpoint.
    def post(self, request, provider):
        if not provider_is_available(provider):  # Unknown or disabled provider: no receiver here.
            raise NotFound()
        try:  # Duplicate events are expected and should be acknowledged.
            event = webhooks.ingest_webhook(  # Hand the raw signed request to the webhook ingestion layer.
                provider=provider, raw_body=request.body, headers=dict(request.headers),
            )
        except DuplicateWebhookError:  # Already processed; acknowledge so the provider stops retrying.
            # Already handled - acknowledge so the provider stops retrying.
            return success_response("Duplicate event ignored.", data={"duplicate": True})
        return success_response(
            "Webhook processed.", data={"id": event.id, "status": event.status},
        )


# --------------------------------------------------------------------------- #
# Inbound webhooks that need an operator                                       #
# --------------------------------------------------------------------------- #

#: Webhook states that mean "money moved at the provider and we did not record it".
NEEDS_ATTENTION_STATUSES = (WebhookStatus.FAILED, WebhookStatus.IGNORED)


class WebhookEventListView(APIView):
    """GET /payments/webhooks/ - inbound provider events, newest first.

    Defaults to the ones that need an operator: FAILED (we tried to book and could
    not) and IGNORED (valid signature, nothing local to match). Money has usually
    moved at the provider by then, so without this list a failed booking is visible
    only to someone querying the table by hand.

    Filters: ``?status=`` (a single WebhookStatus, or ``ALL``), ``?provider=``,
    ``?search=`` over the provider reference and the matched record's reference.

    docstring-name: Provider webhooks
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.webhook.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        _, reach = _reach(request)
        qs = reach.webhooks().select_related("collection__customer", "payout")

        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter == "ALL":
            pass  # Explicitly asked for the whole history, not just the problems.
        elif status_filter:
            qs = qs.filter(status=status_filter)
        else:
            qs = qs.filter(status__in=NEEDS_ATTENTION_STATUSES)

        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                Q(provider_reference__icontains=search)
                | Q(collection__reference__icontains=search)
                | Q(payout__reference__icontains=search),
            )
        return _paginate(request, qs.order_by("-created_at", "-id"),
                         WebhookEventSerializer, self)


class WebhookEventSummaryView(APIView):
    """GET /payments/webhooks/summary/ - how many events are waiting on an operator.

    Small on purpose: it exists so a console can badge the problem without pulling a
    page of rows, and so "nothing to see" is a cheap, honest answer.

    docstring-name: Provider webhooks summary
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.webhook.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        _, reach = _reach(request)
        qs = reach.webhooks().select_related("collection__customer", "payout")
        counts = {
            row["status"]: row["count"]
            for row in qs.values("status").annotate(count=Count("id"))
        }
        return success_response("Webhook summary retrieved.", data={
            "failed": counts.get(WebhookStatus.FAILED, 0),
            "ignored": counts.get(WebhookStatus.IGNORED, 0),
            "needs_attention": sum(
                counts.get(status, 0) for status in NEEDS_ATTENTION_STATUSES),
            "status_counts": counts,
        })


# Re-run one stored event and describe what happened.
def _replay_event(event):
    """Replay ``event`` and return the operator-facing response for it.

    Shared by the entity-scoped and the platform-scope replay endpoints: which events a
    caller may reach differs, what a replay *does* does not, and the behaviour below
    (the already-processed short-circuit, the honest "did not succeed" message) is the
    part that must not drift between the two screens.

    Re-runs the same :func:`vs_payments.webhooks.process_stored_event` the task uses,
    against the body already on file. Safe to press twice: the confirm services are
    idempotent on a terminal record, and the processor itself no-ops on an event that
    already reached PROCESSED, so a replay can neither double-book nor undo one.
    """
    if event.status == WebhookStatus.PROCESSED:
        return success_response(
            "This event was already processed; nothing to replay.",
            data=WebhookEventSerializer(event).data,
        )

    webhooks.process_stored_event(event.id)
    event.refresh_from_db()
    booked = event.status == WebhookStatus.PROCESSED
    return success_response(
        "Webhook replayed and booked." if booked
        else f"Replay did not succeed: {event.error or 'see the event for detail'}.",
        data=WebhookEventSerializer(event).data,
    )


class WebhookEventReplayView(APIView):
    """POST /payments/webhooks/<id>/replay/ - re-run a stored event.

    The usual reason a replay now succeeds is that the blocker has gone - most often a
    fiscal period that was closed when the event first arrived and has since reopened.

    docstring-name: Replay a provider webhook
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.webhook.replay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _, reach = _reach(request)
        event = reach.get_or_404(
            reach.webhooks().select_related("collection__customer", "payout"),
            pk, "Webhook event not found for this entity.")
        return _replay_event(event)


# --------------------------------------------------------------------------- #
# Unattributed webhooks (platform scope, CX staff only)                        #
# --------------------------------------------------------------------------- #

# Restrict webhook events to the ones that belong to nobody.
def _unattributed_webhooks():
    """Webhook events that matched neither a collection nor a payout.

    The exact complement of :meth:`vs_payments.reach.PaymentsReach.webhooks`: an event links to a collection or
    a payout (and is then shown on that entity's screen) or it links to neither, in
    which case there is no entity to scope it to and no tenant may be shown it. What is
    left is genuine debris - a staging PSP pointed at production, a reference that no
    longer exists, an event type we do not handle - but it is still money moving at the
    provider against a reference we do not recognise, so somebody has to see it.
    """
    return WebhookEvent.objects.filter(collection__isnull=True, payout__isnull=True)


# Group the platform-only gate for the unattributed-webhook endpoints.
class _PlatformWebhookView(APIView):
    """Base for the unattributed-event endpoints: CX staff, and no ``?entity=``.

    These rows have no entity, so :func:`vs_finance.views.resolve_entity` - the normal
    "does this entity belong to your tenant" gate - has nothing to resolve and cannot be
    the authorisation here. The gate instead is ``IsVisionStaff``, the platform's
    existing convention for a CX-only surface (``vs_todo``, ``vs_schools`` lifecycle,
    the security screens all use it): it passes only when the *effective* user's home
    tenant is the PLATFORM (Codex) tenant, so a school user is refused however their
    roles are configured, and a CX staffer proxying as a school user is refused too
    (while proxying they are that school's user and must see what that user sees).

    ``IsVisionStaff`` answers "may this person stand at the platform level at all";
    ``HasRBACPermission`` answers "may they do this particular thing". Both are
    required: the entity-scoped ``payments.webhook.*`` grants deliberately do not
    reach here, because they are a tenant's licence over its own books.
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]


class UnattributedWebhookListView(_PlatformWebhookView):
    """GET /payments/webhooks/unattributed/ - provider events that belong to no tenant.

    Expect this list to be short and usually empty; that is the healthy state, not a
    reason to alarm anyone. Defaults, like the entity-scoped list, to the events that
    reached a terminal state needing a human: IGNORED (valid signature, nothing local
    to match) and FAILED. ``?status=ALL`` widens it to everything unattributed, which
    also surfaces RECEIVED rows still waiting on a worker.

    Filters: ``?status=`` (a single WebhookStatus, or ``ALL``), ``?provider=``,
    ``?search=`` over the provider reference (there is no matched record to search).

    docstring-name: Unattributed provider webhooks
    """

    rbac_permission = "payments.unattributed_webhook.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        qs = _unattributed_webhooks()

        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter == "ALL":
            pass  # Explicitly asked for everything unattributed, not just the problems.
        elif status_filter:
            qs = qs.filter(status=status_filter)
        else:
            qs = qs.filter(status__in=NEEDS_ATTENTION_STATUSES)

        if (provider := request.query_params.get("provider")):
            qs = qs.filter(provider=provider)
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(provider_reference__icontains=search)
        return _paginate(request, qs.order_by("-created_at", "-id"),
                         WebhookEventSerializer, self)


class UnattributedWebhookReplayView(_PlatformWebhookView):
    """POST /payments/webhooks/unattributed/<id>/replay/ - re-run an unattributed event.

    Same replay as the entity-scoped one (:func:`_replay_event`), reached differently.
    It is worth pressing after the reason for the mismatch has been fixed: provision the
    virtual account the deposit named, for instance, and the replay resolves the payer
    and books the receipt. The event then has a collection, so it leaves this list and
    appears on that entity's own screen - which is where it belonged all along.

    Only ever finds an unattributed event: an id that has since been matched (or never
    was) resolves to nothing here, so this endpoint can never be used to reach into a
    tenant's own events.

    docstring-name: Replay an unattributed provider webhook
    """

    rbac_permission = "payments.unattributed_webhook.replay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        event = _unattributed_webhooks().filter(pk=pk).first()
        if event is None:
            raise NotFound("No unattributed webhook event matches that id.")
        return _replay_event(event)
