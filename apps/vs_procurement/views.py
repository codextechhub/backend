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

from . import approvals, contracts, payables, purchasing, sourcing, stock
from .models import (
    CatalogItem,
    ContractMilestone,
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    StockItem,
    StockMovement,
    Vendor,
    VendorCategory,
    VendorContract,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
    VendorQuotation,
    VendorQuotationLine,
)
from .serializers import (
    CatalogItemSerializer,
    GoodsReceivedNoteSerializer,
    PurchaseOrderSerializer,
    RequestForQuotationSerializer,
    RequisitionSerializer,
    StockItemSerializer,
    StockMovementSerializer,
    VendorCategorySerializer,
    VendorContractSerializer,
    VendorInvoiceSerializer,
    VendorPaymentSerializer,
    VendorQuotationSerializer,
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
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

def _build_milestones(contract, items):
    """Create ContractMilestone rows from a request ``milestones`` list (optional)."""
    for i, ms in enumerate(items or [], start=1):
        if not ms.get("name"):
            raise ValidationError({"milestones": "Each milestone needs a name."})
        ContractMilestone.objects.create(
            contract=contract, line_no=ms.get("line_no", i), name=ms["name"],
            due_date=_date(ms.get("due_date"), "due_date"),
            amount=_money(ms.get("amount", 0), "amount"),
            note=ms.get("note", ""),
        )


class ContractListCreateView(_ProcBase):
    """GET (list) / POST (create a DRAFT contract + optional milestones)."""

    @property
    def rbac_permission(self):
        return "procurement.contract.create" if self.request.method == "POST" \
            else "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorContract.objects.filter(entity=entity).select_related(
            "vendor").prefetch_related("milestones")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(vendor__code=vendor)
        return success_response(
            "Vendor contracts retrieved.",
            data=VendorContractSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("reference") or not body.get("title"):
            raise ValidationError({"reference": "reference and title are required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        contract = VendorContract.objects.create(
            entity=entity, vendor=vendor,
            reference=body["reference"], title=body["title"],
            start_date=_date(body.get("start_date"), "start_date"),
            end_date=_date(body.get("end_date"), "end_date"),
            contract_value=_money(body.get("contract_value", 0), "contract_value"),
            payment_terms=body.get("payment_terms") or "NET_30",
            auto_renew=bool(body.get("auto_renew", False)),
            renewal_notice_days=body.get("renewal_notice_days") or 30,
            notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _build_milestones(contract, body.get("milestones"))
        return success_response(
            "Vendor contract created.", data=VendorContractSerializer(contract).data, status=201,
        )


class ContractDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update header fields) one contract."""

    @property
    def rbac_permission(self):
        return "procurement.contract.update" if self.request.method == "PATCH" \
            else "procurement.contract.view"

    def _get(self, entity, pk):
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract

    def get(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        return success_response("Vendor contract retrieved.", data=VendorContractSerializer(contract).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        body = request.data
        if "title" in body:
            contract.title = body["title"]
        if "start_date" in body:
            contract.start_date = _date(body.get("start_date"), "start_date")
        if "end_date" in body:
            contract.end_date = _date(body.get("end_date"), "end_date")
        if "contract_value" in body:
            contract.contract_value = _money(body.get("contract_value", 0), "contract_value")
        if "payment_terms" in body:
            contract.payment_terms = body["payment_terms"] or "NET_30"
        if "auto_renew" in body:
            contract.auto_renew = bool(body["auto_renew"])
        if "renewal_notice_days" in body:
            contract.renewal_notice_days = body.get("renewal_notice_days") or 30
        if "notes" in body:
            contract.notes = body["notes"]
        contract.save()
        return success_response("Vendor contract updated.", data=VendorContractSerializer(contract).data)


class _ContractActionBase(_ProcBase):
    def _get(self, request, pk):
        entity = resolve_entity(request)
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract


class ContractActivateView(_ContractActionBase):
    rbac_permission = "procurement.contract.activate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.activate_contract(contract, actor_user=request.user)
        return success_response("Vendor contract activated.", data=VendorContractSerializer(contract).data)


class ContractTerminateView(_ContractActionBase):
    rbac_permission = "procurement.contract.terminate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.terminate_contract(
            contract, reason=request.data.get("reason", ""), actor_user=request.user)
        return success_response("Vendor contract terminated.", data=VendorContractSerializer(contract).data)


class ContractRenewView(_ContractActionBase):
    """POST — create a successor contract that renews this one (marks this RENEWED)."""

    rbac_permission = "procurement.contract.renew"

    def post(self, request, pk):
        contract = self._get(request, pk)
        body = request.data
        if not body.get("reference"):
            raise ValidationError({"reference": "A reference for the renewal contract is required."})
        value = None
        if "contract_value" in body:
            value = _money(body.get("contract_value", 0), "contract_value")
        successor = contracts.renew_contract(
            contract, reference=body["reference"],
            start_date=_date(body.get("start_date"), "start_date", required=True),
            end_date=_date(body.get("end_date"), "end_date", required=True),
            contract_value=value,
            copy_milestones=bool(body.get("copy_milestones", False)),
            actor_user=request.user,
        )
        return success_response(
            f"Contract renewed → {successor.reference}.",
            data=VendorContractSerializer(successor).data, status=201,
        )


class ContractMilestoneCompleteView(_ProcBase):
    """POST — mark a milestone COMPLETED."""

    rbac_permission = "procurement.contract.update"

    def post(self, request, pk, milestone_id):
        entity = resolve_entity(request)
        milestone = ContractMilestone.objects.filter(
            contract__entity=entity, contract_id=pk, pk=milestone_id).first()
        if milestone is None:
            raise NotFound("No such milestone on this contract.")
        contracts.complete_milestone(
            milestone, on=_date(request.data.get("completed_date"), "completed_date"),
            actor_user=request.user,
        )
        milestone.contract.refresh_from_db()
        return success_response(
            "Milestone completed.", data=VendorContractSerializer(milestone.contract).data,
        )


class ContractRenewalsView(_ProcBase):
    """GET — contracts due for renewal (inside their notice window or a ``within_days`` horizon)."""

    rbac_permission = "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        within = request.query_params.get("within_days")
        rows = contracts.expiring_contracts(
            entity, as_of=as_of, within_days=int(within) if within else None,
        )
        return success_response(
            "Contracts due for renewal retrieved.",
            data=VendorContractSerializer(rows, many=True).data,
        )


# --------------------------------------------------------------------------- #
# Item catalog                                                                #
# --------------------------------------------------------------------------- #

def _resolve_catalog_item(entity, ref, field="catalog_item"):
    """Resolve a catalog item by id/code, or ``None`` when ``ref`` is blank."""
    if ref in (None, ""):
        return None
    qs = CatalogItem.objects.filter(entity=entity)
    item = qs.filter(pk=int(ref)).first() if str(ref).isdigit() else qs.filter(code=str(ref)).first()
    if item is None:
        raise ValidationError({field: f"No catalog item '{ref}' in this entity."})
    return item


def _resolve_optional_vendor(entity, ref, field="preferred_vendor"):
    """Resolve a vendor by id/code, or ``None`` when ``ref`` is blank."""
    if ref in (None, ""):
        return None
    qs = Vendor.objects.filter(entity=entity)
    vendor = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref)).first() or qs.filter(code=str(ref).upper()).first()
    )
    if vendor is None:
        raise ValidationError({field: f"No vendor '{ref}' in this entity."})
    return vendor


class CatalogItemListCreateView(_ProcBase):
    """GET (list) / POST (create) catalog items — reusable buying defaults."""

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.create" if self.request.method == "POST" \
            else "procurement.catalog_item.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = CatalogItem.objects.filter(entity=entity).select_related(
            "preferred_vendor", "default_expense_account", "default_tax_code")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(preferred_vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(preferred_vendor__code=vendor)
        if (search := request.query_params.get("q")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return success_response(
            "Catalog items retrieved.",
            data=CatalogItemSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        item = CatalogItem.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            description=body.get("description", ""),
            unit_of_measure=body.get("unit_of_measure") or "each",
            preferred_vendor=_resolve_optional_vendor(entity, body.get("preferred_vendor")),
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            default_tax_code=_resolve_tax(entity, body.get("default_tax_code"), "default_tax_code"),
            lead_time_days=body.get("lead_time_days") or None,
            standard_unit_price=_money(body.get("standard_unit_price", 0), "standard_unit_price"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Catalog item created.", data=CatalogItemSerializer(item).data, status=201,
        )


class CatalogItemDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update buying defaults) one catalog item."""

    @property
    def rbac_permission(self):
        return "procurement.catalog_item.update" if self.request.method == "PATCH" \
            else "procurement.catalog_item.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        return success_response("Catalog item retrieved.", data=CatalogItemSerializer(item).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = CatalogItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such catalog item in this entity.")
        body = request.data
        if "name" in body:
            item.name = body["name"]
        if "description" in body:
            item.description = body["description"]
        if "unit_of_measure" in body:
            item.unit_of_measure = body["unit_of_measure"] or "each"
        if "preferred_vendor" in body:
            item.preferred_vendor = _resolve_optional_vendor(entity, body.get("preferred_vendor"))
        if "default_expense_account" in body:
            item.default_expense_account = _resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "default_tax_code" in body:
            item.default_tax_code = _resolve_tax(entity, body.get("default_tax_code"), "default_tax_code")
        if "lead_time_days" in body:
            item.lead_time_days = body.get("lead_time_days") or None
        if "standard_unit_price" in body:
            item.standard_unit_price = _money(body.get("standard_unit_price", 0), "standard_unit_price")
        if "is_active" in body:
            item.is_active = bool(body["is_active"])
        item.save()
        return success_response("Catalog item updated.", data=CatalogItemSerializer(item).data)


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
            item = _resolve_catalog_item(entity, ln.get("catalog_item"))
            defaults = item.line_defaults() if item else {}
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or defaults.get("expense_account")
            tax = _resolve_tax(entity, ln.get("tax_code")) or defaults.get("tax_code")
            unit_price = ln.get("estimated_unit_price")
            if unit_price in (None, "") and item is not None:
                unit_price = defaults.get("unit_price", 0)
            PurchaseRequisitionLine.objects.create(
                requisition=req, line_no=ln.get("line_no", i),
                description=ln.get("description") or defaults.get("description", ""),
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                estimated_unit_price=_money(unit_price or 0, "estimated_unit_price"),
                expense_account=expense, tax_code=tax,
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
    """Submit a requisition into the ``vs_workflow`` approval engine.

    Approval is no longer a direct endpoint — submitting hands the document to its
    threshold-gated workflow template; approvers then vote through the ``vs_workflow``
    API, and the engine's callback drives the requisition to APPROVED.
    """
    rbac_permission = "procurement.requisition.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        req = PurchaseRequisition.objects.filter(entity=entity, pk=pk).first()
        if req is None:
            raise NotFound("No such requisition in this entity.")
        instance = approvals.submit_for_approval(req, actor_user=request.user)
        return _approval_response("Requisition submitted for approval.",
                                  req, instance, RequisitionSerializer)


# --------------------------------------------------------------------------- #
# Spend approvals (vs_workflow hand-off)                                       #
# --------------------------------------------------------------------------- #

def _approval_response(message, document, instance, serializer_cls):
    """Build the standard envelope for a submit-for-approval action.

    Re-reads ``document`` because the engine may have reached a terminal decision
    synchronously (all stages auto-skipped), mutating it via a different instance.
    """
    document.refresh_from_db()
    return success_response(message, data={
        "workflow_instance_id": instance.id,
        "workflow_status": instance.status,
        "approval_state": document.approval_state,
        "document": serializer_cls(document).data,
    })


class PurchaseOrderSubmitApprovalView(_ProcBase):
    rbac_permission = "procurement.purchase_order.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        po = PurchaseOrder.objects.filter(entity=entity, pk=pk).first()
        if po is None:
            raise NotFound("No such purchase order in this entity.")
        instance = approvals.submit_for_approval(po, actor_user=request.user)
        return _approval_response("Purchase order submitted for approval.",
                                  po, instance, PurchaseOrderSerializer)


class VendorInvoiceSubmitApprovalView(_ProcBase):
    rbac_permission = "procurement.vendor_invoice.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        inv = VendorInvoice.objects.filter(entity=entity, pk=pk).first()
        if inv is None:
            raise NotFound("No such vendor invoice in this entity.")
        instance = approvals.submit_for_approval(inv, actor_user=request.user)
        return _approval_response("Vendor invoice submitted for approval.",
                                  inv, instance, VendorInvoiceSerializer)


class ApprovalTemplateSetupView(_ProcBase):
    """Provision the platform-wide default threshold-gated approval templates.

    POST body (all optional): ``threshold`` (kobo), ``manager_permission``,
    ``senior_permission``. Idempotent — re-running upserts the templates in place.
    """
    rbac_permission = "procurement.approval.manage"

    def post(self, request):
        body = request.data or {}
        kwargs = {}
        if "threshold" in body:
            kwargs["threshold"] = _money(body.get("threshold"), "threshold")
        if body.get("manager_permission"):
            kwargs["manager_permission"] = str(body["manager_permission"])
        if body.get("senior_permission"):
            kwargs["senior_permission"] = str(body["senior_permission"])
        templates = approvals.ensure_default_approval_templates(
            created_by=request.user, **kwargs,
        )
        return success_response("Default approval templates provisioned.", data={
            "templates": [
                {"id": t.id, "document_type": t.document_type, "code": t.code, "name": t.name}
                for t in templates
            ],
        })


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
# Requests for quotation (sourcing)                                           #
# --------------------------------------------------------------------------- #

class RfqListCreateView(_ProcBase):
    """GET (list) / POST (create draft RFQ + lines)."""

    @property
    def rbac_permission(self):
        return "procurement.rfq.create" if self.request.method == "POST" \
            else "procurement.rfq.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = RequestForQuotation.objects.filter(entity=entity).prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(rfq_status=status_)
        return success_response(
            "RFQs retrieved.",
            data=RequestForQuotationSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        requisition = None
        if body.get("requisition"):
            requisition = PurchaseRequisition.objects.filter(
                entity=entity, pk=body["requisition"]).first()
            if requisition is None:
                raise ValidationError({"requisition": "No such requisition in this entity."})
        rfq = RequestForQuotation.objects.create(
            entity=entity, requisition=requisition,
            title=body.get("title", ""),
            issue_date=_date(body.get("issue_date"), "issue_date", required=True),
            response_due_date=_date(body.get("response_due_date"), "response_due_date"),
            notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            req_line = None
            if ln.get("requisition_line"):
                req_line = PurchaseRequisitionLine.objects.filter(
                    requisition__entity=entity, pk=ln["requisition_line"]).first()
                if req_line is None:
                    raise ValidationError(
                        {"requisition_line": f"No such requisition line {ln['requisition_line']}."})
            RfqLine.objects.create(
                rfq=rfq, line_no=ln.get("line_no", i),
                description=ln.get("description", ""),
                quantity=_dec(ln.get("quantity", 1), "quantity"),
                requisition_line=req_line,
                expense_account=_resolve_account(entity, ln.get("expense_account"), "expense_account"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        return success_response(
            "RFQ created.", data=RequestForQuotationSerializer(rfq).data, status=201,
        )


class RfqDetailView(_ProcBase):
    rbac_permission = "procurement.rfq.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        return success_response("RFQ retrieved.", data=RequestForQuotationSerializer(rfq).data)


class RfqIssueView(_ProcBase):
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.issue_rfq(rfq, actor_user=request.user)
        return success_response("RFQ issued.", data=RequestForQuotationSerializer(rfq).data)


class RfqCancelView(_ProcBase):
    rbac_permission = "procurement.rfq.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=pk).first()
        if rfq is None:
            raise NotFound("No such RFQ in this entity.")
        sourcing.cancel_rfq(rfq, reason=request.data.get("reason", ""), actor_user=request.user)
        return success_response("RFQ cancelled.", data=RequestForQuotationSerializer(rfq).data)


# --------------------------------------------------------------------------- #
# Vendor quotations (sourcing)                                                #
# --------------------------------------------------------------------------- #

class QuotationListCreateView(_ProcBase):
    """GET (list) / POST (create draft quotation + priced lines) against an RFQ."""

    @property
    def rbac_permission(self):
        return "procurement.quotation.create" if self.request.method == "POST" \
            else "procurement.quotation.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorQuotation.objects.filter(entity=entity).select_related(
            "vendor", "rfq").prefetch_related("lines")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(quotation_status=status_)
        if (rfq := request.query_params.get("rfq")):
            qs = qs.filter(rfq_id=rfq)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() else qs.filter(vendor__code=vendor)
        return success_response(
            "Quotations retrieved.",
            data=VendorQuotationSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        lines = _require_lines(body)
        rfq = RequestForQuotation.objects.filter(entity=entity, pk=body.get("rfq")).first()
        if rfq is None:
            raise ValidationError({"rfq": "An RFQ is required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        quotation = VendorQuotation.objects.create(
            entity=entity, rfq=rfq, vendor=vendor,
            quote_date=_date(body.get("quote_date"), "quote_date", required=True),
            valid_until=_date(body.get("valid_until"), "valid_until"),
            currency=_resolve_currency(entity, body.get("currency")),
            lead_time_days=body.get("lead_time_days") or None,
            reference=body.get("reference", ""), notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        for i, ln in enumerate(lines, start=1):
            rfq_line = None
            if ln.get("rfq_line"):
                rfq_line = RfqLine.objects.filter(rfq__entity=entity, pk=ln["rfq_line"]).first()
                if rfq_line is None:
                    raise ValidationError({"rfq_line": f"No such RFQ line {ln['rfq_line']}."})
            expense = _resolve_account(entity, ln.get("expense_account"), "expense_account") \
                or (rfq_line.expense_account if rfq_line else None)
            VendorQuotationLine.objects.create(
                quotation=quotation, rfq_line=rfq_line, line_no=ln.get("line_no", i),
                description=ln.get("description", rfq_line.description if rfq_line else ""),
                expense_account=expense,
                quantity=_dec(ln.get("quantity", rfq_line.quantity if rfq_line else 1), "quantity"),
                unit_price=_money(ln.get("unit_price", 0), "unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code")),
            )
        sourcing.price_quotation(quotation)
        return success_response(
            "Quotation created.", data=VendorQuotationSerializer(quotation).data, status=201,
        )


class QuotationDetailView(_ProcBase):
    rbac_permission = "procurement.quotation.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        return success_response("Quotation retrieved.", data=VendorQuotationSerializer(quotation).data)


class QuotationSubmitView(_ProcBase):
    rbac_permission = "procurement.quotation.submit"

    def post(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        sourcing.submit_quotation(quotation, actor_user=request.user)
        quotation.refresh_from_db()
        return success_response(
            "Quotation submitted.", data=VendorQuotationSerializer(quotation).data,
        )


class QuotationAwardView(_ProcBase):
    """POST — award the quotation: build a DRAFT PO and reject the losing quotes."""

    rbac_permission = "procurement.quotation.award"

    def post(self, request, pk):
        entity = resolve_entity(request)
        quotation = VendorQuotation.objects.filter(entity=entity, pk=pk).first()
        if quotation is None:
            raise NotFound("No such quotation in this entity.")
        po = sourcing.award_quotation(
            quotation,
            order_date=_date(request.data.get("order_date"), "order_date"),
            actor_user=request.user,
        )
        return success_response(
            f"Quotation awarded → purchase order {po.document_number}.",
            data=PurchaseOrderSerializer(po).data, status=201,
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


class APCashRequirementsView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import FORECAST_BUCKETS, ap_cash_requirements

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = ap_cash_requirements(entity, as_of=as_of)
        return success_response(
            "AP cash-requirements forecast retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(FORECAST_BUCKETS),
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                        "buckets": {b: _kobo(v) for b, v in r.buckets.items()},
                        "total": _kobo(r.total),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_due": _kobo(report.total_due),
            },
        )


class GRIRAgingView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import AGING_BUCKETS, grir_aging

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        report = grir_aging(entity, as_of=as_of)
        return success_response(
            "GR/IR aging retrieved.",
            data={
                "entity": entity.code, "as_of": str(report.as_of),
                "buckets": list(AGING_BUCKETS),
                "rows": [
                    {
                        "grn_id": r.grn_id, "reference": r.reference,
                        "vendor_code": r.vendor_code, "vendor_name": r.vendor_name,
                        "received_date": str(r.received_date), "days": r.days,
                        "bucket": r.bucket,
                        "received_value": _kobo(r.received_value),
                        "invoiced_value": _kobo(r.invoiced_value),
                        "open_value": _kobo(r.open_value),
                    }
                    for r in report.rows
                ],
                "bucket_totals": {b: _kobo(v) for b, v in report.bucket_totals.items()},
                "total_open": _kobo(report.total_open),
                "control_balance": _kobo(report.control_balance),
                "difference": _kobo(report.difference),
            },
        )


# --------------------------------------------------------------------------- #
# Procurement analytics                                                        #
# --------------------------------------------------------------------------- #

class SpendAnalysisView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import spend_analysis

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        report = spend_analysis(entity, start_date=start, end_date=end)

        def _rows(rows):
            return [
                {
                    "key": r.key, "label": r.label,
                    "net": _kobo(r.net), "tax": _kobo(r.tax), "gross": _kobo(r.gross),
                    "invoice_count": r.invoice_count,
                }
                for r in rows
            ]

        return success_response(
            "Spend analysis retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "by_vendor": _rows(report.by_vendor),
                "by_category": _rows(report.by_category),
                "total_net": _kobo(report.total_net),
                "total_tax": _kobo(report.total_tax),
                "total_gross": _kobo(report.total_gross),
                "invoice_count": report.invoice_count,
            },
        )


class VendorPerformanceView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import vendor_performance

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        report = vendor_performance(entity, start_date=start, end_date=end)
        return success_response(
            "Vendor performance retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "rows": [
                    {
                        "vendor_id": r.vendor_id, "code": r.code, "name": r.name,
                        "po_count": r.po_count, "total_ordered": _kobo(r.total_ordered),
                        "receipt_count": r.receipt_count,
                        "on_time_receipts": r.on_time_receipts,
                        "late_receipts": r.late_receipts,
                        "on_time_rate": r.on_time_rate,
                        "invoice_count": r.invoice_count,
                        "total_billed": _kobo(r.total_billed),
                        "payment_count": r.payment_count,
                        "total_paid": _kobo(r.total_paid),
                        "avg_payment_days": r.avg_payment_days,
                    }
                    for r in report.rows
                ],
            },
        )


class ProcurementCycleTimeView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        from .reports import procurement_cycle_time

        entity = resolve_entity(request)
        start = _date(request.query_params.get("start_date"), "start_date")
        end = _date(request.query_params.get("end_date"), "end_date")
        report = procurement_cycle_time(entity, start_date=start, end_date=end)
        return success_response(
            "Procurement cycle time retrieved.",
            data={
                "entity": entity.code,
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "stages": [
                    {
                        "name": s.name, "label": s.label,
                        "sample_count": s.sample_count, "avg_days": s.avg_days,
                    }
                    for s in report.stages
                ],
                "end_to_end_avg_days": report.end_to_end_avg_days,
                "end_to_end_count": report.end_to_end_count,
            },
        )


# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                     #
# --------------------------------------------------------------------------- #

class StockItemListCreateView(_ProcBase):
    """GET (list) / POST (create) stock items — perpetual-inventory masters."""

    @property
    def rbac_permission(self):
        return "procurement.stock.manage" if self.request.method == "POST" \
            else "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = StockItem.objects.filter(entity=entity).select_related(
            "inventory_account", "default_expense_account", "catalog_item")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("q")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if request.query_params.get("needs_reorder") == "true":
            from django.db.models import F
            qs = qs.filter(is_active=True, on_hand_qty__lte=F("reorder_level"))
        return success_response(
            "Stock items retrieved.",
            data=StockItemSerializer(qs.order_by("code")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("code") or not body.get("name"):
            raise ValidationError({"code": "code and name are required."})
        inventory = _resolve_account(
            entity, body.get("inventory_account"), "inventory_account")
        if inventory is None:
            raise ValidationError(
                {"inventory_account": "An inventory asset account is required."})
        item = StockItem.objects.create(
            entity=entity, code=body["code"], name=body["name"],
            description=body.get("description", ""),
            unit_of_measure=body.get("unit_of_measure") or "each",
            catalog_item=_resolve_catalog_item(entity, body.get("catalog_item")),
            inventory_account=inventory,
            default_expense_account=_resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account"),
            reorder_level=_dec(body.get("reorder_level", 0), "reorder_level"),
            reorder_qty=_dec(body.get("reorder_qty", 0), "reorder_qty"),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            "Stock item created.", data=StockItemSerializer(item).data, status=201,
        )


class StockItemDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update master fields, not balances) one stock item."""

    @property
    def rbac_permission(self):
        return "procurement.stock.manage" if self.request.method == "PATCH" \
            else "procurement.stock.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        return success_response("Stock item retrieved.", data=StockItemSerializer(item).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        if "name" in body:
            item.name = body["name"]
        if "description" in body:
            item.description = body["description"]
        if "unit_of_measure" in body:
            item.unit_of_measure = body["unit_of_measure"] or "each"
        if "catalog_item" in body:
            item.catalog_item = _resolve_catalog_item(entity, body.get("catalog_item"))
        if "inventory_account" in body:
            inv = _resolve_account(entity, body.get("inventory_account"), "inventory_account")
            if inv is None:
                raise ValidationError(
                    {"inventory_account": "An inventory asset account is required."})
            item.inventory_account = inv
        if "default_expense_account" in body:
            item.default_expense_account = _resolve_account(
                entity, body.get("default_expense_account"), "default_expense_account")
        if "reorder_level" in body:
            item.reorder_level = _dec(body.get("reorder_level", 0), "reorder_level")
        if "reorder_qty" in body:
            item.reorder_qty = _dec(body.get("reorder_qty", 0), "reorder_qty")
        if "is_active" in body:
            item.is_active = bool(body["is_active"])
        item.save()
        return success_response("Stock item updated.", data=StockItemSerializer(item).data)


class StockIssueView(_ProcBase):
    """POST — issue stock out at moving-average cost (Dr expense, Cr inventory)."""

    rbac_permission = "procurement.stock.issue"

    def post(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        movement = stock.issue_stock(
            item,
            quantity=_dec(body.get("quantity"), "quantity"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            expense_account=_resolve_account(
                entity, body.get("expense_account"), "expense_account"),
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        item.refresh_from_db()
        return success_response(
            "Stock issued.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemSerializer(item).data,
            },
            status=201,
        )


class StockAdjustView(_ProcBase):
    """POST — apply a signed stock-count correction (write-up or shrinkage)."""

    rbac_permission = "procurement.stock.adjust"

    def post(self, request, pk):
        entity = resolve_entity(request)
        item = StockItem.objects.filter(entity=entity, pk=pk).first()
        if item is None:
            raise NotFound("No such stock item in this entity.")
        body = request.data
        unit_cost = body.get("unit_cost")
        movement = stock.adjust_stock(
            item,
            quantity_delta=_dec(body.get("quantity_delta"), "quantity_delta"),
            movement_date=_date(body.get("movement_date"), "movement_date")
            or datetime.date.today(),
            adjustment_account=_resolve_account(
                entity, body.get("adjustment_account"), "adjustment_account"),
            unit_cost=_money(unit_cost, "unit_cost") if unit_cost not in (None, "") else None,
            actor_user=request.user,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
        )
        item.refresh_from_db()
        return success_response(
            "Stock adjusted.",
            data={
                "movement": StockMovementSerializer(movement).data,
                "stock_item": StockItemSerializer(item).data,
            },
            status=201,
        )


class StockMovementListView(_ProcBase):
    """GET — the stock ledger (movements), optionally filtered to one item."""

    rbac_permission = "procurement.stock.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = StockMovement.objects.filter(entity=entity).select_related("stock_item")
        if (item_ref := request.query_params.get("stock_item")):
            qs = qs.filter(stock_item_id=item_ref) if str(item_ref).isdigit() \
                else qs.filter(stock_item__code=item_ref)
        if (mtype := request.query_params.get("movement_type")):
            qs = qs.filter(movement_type=mtype)
        return success_response(
            "Stock movements retrieved.",
            data=StockMovementSerializer(qs[:300], many=True).data,
        )


class StockReorderReportView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        rows = stock.reorder_report(entity)
        return success_response(
            "Stock reorder report retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        "stock_item_id": r["stock_item_id"], "code": r["code"],
                        "name": r["name"], "on_hand_qty": str(r["on_hand_qty"]),
                        "reorder_level": str(r["reorder_level"]),
                        "reorder_qty": str(r["reorder_qty"]),
                        "unit_cost": _kobo(r["unit_cost"]),
                    }
                    for r in rows
                ],
            },
        )


class StockValuationReportView(_ProcBase):
    rbac_permission = "procurement.report.view"

    def get(self, request):
        entity = resolve_entity(request)
        report = stock.stock_valuation(entity)
        return success_response(
            "Stock valuation retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        "stock_item_id": r["stock_item_id"], "code": r["code"],
                        "name": r["name"], "on_hand_qty": str(r["on_hand_qty"]),
                        "unit_cost": _kobo(r["unit_cost"]),
                        "stock_value": _kobo(r["stock_value"]),
                    }
                    for r in report["rows"]
                ],
                "total_value": _kobo(report["total_value"]),
            },
        )
