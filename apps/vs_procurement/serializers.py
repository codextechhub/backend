"""DRF serializers for the vs_procurement REST API (the Procure-to-Pay surface).

Read-side serialisers for the AP sub-ledger and the purchasing chain
(PR → PO → GRN → vendor invoice → vendor payment). Document *creation* is handled in
the views (they parse the request, resolve GL accounts by code/id and call the
purchasing/payables services), so these serialisers stay read-only - mirroring how
``vs_finance`` serialises its documents while the services own the writes.

Money is always integer kobo; each headline money field is mirrored with a ``*_naira``
display string so a client never needs to know the divisor.
"""
from __future__ import annotations

from rest_framework import serializers
from django.utils import timezone

from vs_finance.constants import DocumentStatus
from vs_finance.money import format_naira
from vs_rbac.fls import FieldSecurityMixin
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

from .constants import ProcApprovalState, QuotationStatus
from .purchasing import po_receipt_stage
from .models import (
    CatalogItem,
    ContractMilestone,
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderVendorDelivery,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequestForQuotation,
    RfqLine,
    StockBalance,
    StockItem,
    StockLocation,
    StockMovement,
    Vendor,
    VendorContact,
    VendorCategory,
    VendorContract,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
    VendorPaymentAllocation,
    VendorQuotation,
    VendorQuotationLine,
)


# --------------------------------------------------------------------------- #
# Master data                                                                 #
# --------------------------------------------------------------------------- #

class VendorCategorySerializer(serializers.ModelSerializer):
    """Category tree row with queryset-provided usage counts and parent labels.

    The view selects both parent levels and annotates the three counts. Keeping those
    values read-only prevents serializer evaluation from turning a category list into
    recursive count/parent queries.
    """

    default_expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )
    vendor_count = serializers.IntegerField(read_only=True, default=0)
    child_count = serializers.IntegerField(read_only=True, default=0)
    catalog_item_count = serializers.IntegerField(read_only=True, default=0)
    parent_code = serializers.CharField(source="parent.code", read_only=True, default=None)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)
    level = serializers.SerializerMethodField()

    def get_level(self, category):
        # Parent and grandparent are select_related by category views, so depth is query-free.
        if category.parent_id is None:
            return 1
        return 2 if category.parent.parent_id is None else 3

    class Meta:
        model = VendorCategory
        fields = [
            "id", "code", "name", "parent_id", "parent_code", "parent_name", "level",
            "default_expense_account_id", "default_expense_code", "is_active",
            "vendor_count", "child_count", "catalog_item_count",
        ]


class VendorContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorContact
        fields = [
            "id", "name", "email", "phone", "is_primary", "receives_rfqs",
            "receives_purchase_orders", "is_active",
        ]


class VendorSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """Vendor detail shape with account labels and field-level sensitive-data gates.

    ``FieldSecurityMixin`` removes protected contact, tax, and banking fields when the
    caller lacks ``procurement.vendor.view_sensitive``; it does not reject the whole
    vendor record, so ordinary purchasing screens retain non-PII master data.
    """

    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    payable_code = serializers.CharField(source="payable_account.code", read_only=True, default=None)
    default_expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )
    default_wht_tax_code_value = serializers.CharField(
        source="default_wht_tax_code.code", read_only=True, default=None,
    )
    contacts = VendorContactSerializer(many=True, read_only=True)

    # FLS: vendor banking details are PII used for disbursement - only holders of
    # the sensitive grant see them; everyone else gets the record with these
    # fields stripped.
    read_permissions = {
        "email": "procurement.vendor.view_sensitive",
        "phone": "procurement.vendor.view_sensitive",
        "address": "procurement.vendor.view_sensitive",
        "tax_id": "procurement.vendor.view_sensitive",
        "bank_name": "procurement.vendor.view_sensitive",
        "bank_account_number": "procurement.vendor.view_sensitive",
        "bank_account_name": "procurement.vendor.view_sensitive",
        "contacts": "procurement.vendor.view_sensitive",
    }

    class Meta:
        model = Vendor
        fields = [
            "id", "code", "name", "category_id", "category_code",
            "email", "phone", "address", "tax_id",
            "bank_name", "bank_account_number", "bank_account_name",
            "payable_account_id", "payable_code",
            "default_expense_account_id", "default_expense_code",
            "default_wht_tax_code_id", "default_wht_tax_code_value",
            "payment_terms", "kyc_status", "risk", "on_hold", "is_active",
            "contacts",
        ]


class VendorListSerializer(serializers.ModelSerializer):
    """Non-sensitive vendor row shape; detail-only fields never leave the list API."""

    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    active_po_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Vendor
        fields = [
            "id", "code", "name", "category_id", "category_code",
            "payment_terms", "kyc_status", "risk", "on_hold", "is_active",
            "active_po_count",
        ]


# --------------------------------------------------------------------------- #
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

class ContractMilestoneSerializer(serializers.ModelSerializer):
    """Contract checkpoint with its kobo amount mirrored as a Naira display string."""

    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = ContractMilestone
        fields = [
            "id", "line_no", "name", "due_date", "amount", "amount_naira",
            "status", "completed_date", "note",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


def _contract_is_expired(contract) -> bool:
    """Display-only overlay: an ACTIVE contract whose end_date has already passed.

    Computed against today so the list/detail can flag a lapsed contract without a
    scheduler rewriting its authoritative status (``mark_expired`` remains a batch job).
    """
    from .constants import ContractStatus

    return bool(
        contract.status == ContractStatus.ACTIVE
        and contract.end_date is not None
        and contract.end_date < timezone.localdate()
    )


def _contract_activity(entity, contract_id):
    """Finance-audit activity feed for one contract (same shape as the invoice drawer).

    The immutable :class:`FinanceAuditLog` rows targeting this exact contract, newest
    first, capped. Contract lifecycle *and* milestone-completion events both record with
    ``target=contract``, so they all land here. Only called on single-record detail reads.
    """
    from vs_finance.models import FinanceAuditLog

    return [{
        "id": log.id, "action": log.action, "message": log.message, "status": log.status,
        "actor_name": (
            f"{getattr(log.actor, 'first_name', '')} {getattr(log.actor, 'last_name', '')}".strip()
            or getattr(log.actor, "email", "System")
        ) if log.actor_id else "System",
        "created_at": log.created_at,
    } for log in FinanceAuditLog.objects.filter(
        entity=entity, target_type="VendorContract", target_id=str(contract_id),
    ).select_related("actor").order_by("-created_at")[:20]]


class VendorContractListSerializer(serializers.ModelSerializer):
    """Lean contract list row. ``milestone_count`` is a queryset annotation (see
    :meth:`ContractListCreateView.get`) - never a per-row count, so the list stays O(1).
    ``is_expired`` is a computed display overlay, never a persisted status."""

    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    milestone_count = serializers.IntegerField(read_only=True)
    is_expired = serializers.SerializerMethodField()
    contract_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = VendorContract
        fields = [
            "id", "reference", "title", "status",
            "vendor_id", "vendor_code", "vendor_name",
            "start_date", "end_date",
            "contract_value", "contract_value_naira", "payment_terms",
            "auto_renew", "milestone_count", "is_expired",
        ]

    def get_is_expired(self, obj) -> bool:
        return _contract_is_expired(obj)

    def get_contract_value_naira(self, obj) -> str:
        return format_naira(obj.contract_value)


class VendorContractSerializer(serializers.ModelSerializer):
    """Full contract record for the detail drawer: header, milestones, renewal chain
    and the finance-audit activity feed."""

    milestones = ContractMilestoneSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    renewal_window_start = serializers.DateField(read_only=True)
    contract_value_naira = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    renews_reference = serializers.CharField(source="renews.reference", read_only=True, default=None)
    renewed_by_reference = serializers.SerializerMethodField()
    activity = serializers.SerializerMethodField()

    class Meta:
        model = VendorContract
        fields = [
            "id", "reference", "title", "status",
            "vendor_id", "vendor_code", "vendor_name",
            "start_date", "end_date", "renewal_window_start", "is_expired",
            "contract_value", "contract_value_naira", "payment_terms",
            "auto_renew", "renewal_notice_days",
            "renews_id", "renews_reference", "renewed_by_reference", "notes",
            "milestones", "activity",
        ]

    def get_contract_value_naira(self, obj) -> str:
        return format_naira(obj.contract_value)

    def get_is_expired(self, obj) -> bool:
        return _contract_is_expired(obj)

    def get_renewed_by_reference(self, obj) -> str | None:
        # The successor that renews this contract, if one exists (reverse of ``renews``).
        successor = obj.renewed_by.order_by("id").first()
        return successor.reference if successor else None

    def get_activity(self, obj):
        return _contract_activity(obj.entity_id, obj.pk)


# --------------------------------------------------------------------------- #
# Item catalog                                                                #
# --------------------------------------------------------------------------- #

class CatalogItemSerializer(serializers.ModelSerializer):
    """Catalog defaults plus their human-readable related labels and category path.

    ``stock_status`` is an optional queryset annotation used by catalog screens; it is
    not inventory state stored on :class:`CatalogItem` itself.
    """

    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    category_level = serializers.SerializerMethodField()
    category_path = serializers.SerializerMethodField()
    preferred_vendor_code = serializers.CharField(
        source="preferred_vendor.code", read_only=True, default=None,
    )
    preferred_vendor_name = serializers.CharField(
        source="preferred_vendor.name", read_only=True, default=None,
    )
    expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )
    expense_name = serializers.CharField(
        source="default_expense_account.name", read_only=True, default=None,
    )
    tax_code = serializers.CharField(
        source="default_tax_code.code", read_only=True, default=None,
    )
    tax_name = serializers.CharField(
        source="default_tax_code.name", read_only=True, default=None,
    )
    stock_status = serializers.CharField(read_only=True, default=None, allow_null=True)
    standard_unit_price_naira = serializers.SerializerMethodField()

    def get_category_level(self, item):
        """Return the one-based depth from the two-level ``select_related`` chain."""
        if item.category_id is None:
            return None
        if item.category.parent_id is None:
            return 1
        return 2 if item.category.parent.parent_id is None else 3

    def get_category_path(self, item):
        """Render the selected category's root-to-leaf breadcrumb without new queries."""
        if item.category_id is None:
            return None
        nodes = [item.category.name]
        parent = item.category.parent
        while parent is not None:
            nodes.append(parent.name)
            parent = parent.parent
        return " / ".join(reversed(nodes))

    class Meta:
        model = CatalogItem
        fields = [
            "id", "code", "name", "description", "unit_of_measure",
            "category_id", "category_code", "category_name", "category_level", "category_path",
            "preferred_vendor_id", "preferred_vendor_code", "preferred_vendor_name",
            "default_expense_account_id", "expense_code", "expense_name",
            "default_tax_code_id", "tax_code", "tax_name",
            "lead_time_days", "standard_unit_price", "standard_unit_price_naira",
            "is_active", "stock_status",
        ]

    def get_standard_unit_price_naira(self, obj) -> str:
        return format_naira(obj.standard_unit_price)


# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                    #
# --------------------------------------------------------------------------- #

class StockItemListSerializer(serializers.ModelSerializer):
    """Lean stock-item list row. ``unit_cost``/``needs_reorder`` are model properties;
    the ``*_id`` fields carry the picker prefills the edit form re-hydrates from."""

    inventory_code = serializers.CharField(
        source="inventory_account.code", read_only=True, default=None,
    )
    expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )
    catalog_item_code = serializers.CharField(
        source="catalog_item.code", read_only=True, default=None,
    )
    unit_cost = serializers.IntegerField(read_only=True)
    unit_cost_naira = serializers.SerializerMethodField()
    stock_value_naira = serializers.SerializerMethodField()
    needs_reorder = serializers.BooleanField(read_only=True)

    def to_representation(self, instance):
        """Report one store's slice of the item when the list was narrowed to one.

        ``unit_cost`` and ``needs_reorder`` are model properties derived from
        ``on_hand_qty``, ``stock_value`` and ``reorder_level``, so swapping the first
        two is enough to make every figure on the row describe the same place. Doing it
        anywhere else invites the contradiction this exists to avoid: a row selected
        because a store is short of it, reporting a healthy quantity held elsewhere.

        The instance is **never saved** - this is a per-response view of it. The
        balances come from ``location_balances`` in context, resolved once for the whole
        page by the list view rather than a query per row.
        """
        balance = (self.context.get("location_balances") or {}).get(instance.pk)
        if balance is not None:
            instance.on_hand_qty = balance.on_hand_qty
            instance.stock_value = balance.stock_value
        return super().to_representation(instance)

    class Meta:
        model = StockItem
        fields = [
            "id", "code", "name", "description", "unit_of_measure",
            "catalog_item_id", "catalog_item_code",
            "inventory_account_id", "inventory_code",
            "default_expense_account_id", "expense_code",
            "reorder_level", "reorder_qty",
            "on_hand_qty", "stock_value", "stock_value_naira",
            "unit_cost", "unit_cost_naira", "needs_reorder", "is_active",
        ]

    def get_unit_cost_naira(self, obj) -> str:
        return format_naira(obj.unit_cost)

    def get_stock_value_naira(self, obj) -> str:
        return format_naira(obj.stock_value)


class StockItemDetailSerializer(StockItemListSerializer):
    """Full stock-item record for the detail drawer: header, recent movements, activity.

    ``movements`` is the last 50 ledger rows for this item, newest first. The detail
    loader attaches an already SQL-limited list; direct serializer use falls back to an
    equally bounded joined query instead of materialising the full ledger."""

    movements = serializers.SerializerMethodField()
    activity = serializers.SerializerMethodField()

    class Meta(StockItemListSerializer.Meta):
        fields = StockItemListSerializer.Meta.fields + ["movements", "activity"]

    def get_movements(self, obj):
        movements = getattr(obj, "_recent_movements", None)
        if movements is None:
            movements = list(
                obj.movements.select_related("created_by", "stock_item")
                .order_by("-id")[:50]
            )
        return StockMovementSerializer(movements, many=True).data

    def get_activity(self, obj):
        # Finance-audit feed (receipts/issues/adjustments target the stock item).
        return _sourcing_activity(obj.entity_id, "StockItem", obj.pk)


class StockLocationSerializer(serializers.ModelSerializer):
    """A place stock physically sits, and the campus it belongs to if any."""

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )

    class Meta:
        model = StockLocation
        fields = [
            "id", "code", "name", "description",
            "branch_id", "branch_name", "is_default", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class StockBalanceSerializer(serializers.ModelSerializer):
    """What one item holds at one location, with its own weighted-average cost.

    The item's own totals stay the roll-up across locations; these are the rows that
    add up to them.
    """

    stock_item_code = serializers.CharField(
        source="stock_item.code", read_only=True, default=None,
    )
    stock_item_name = serializers.CharField(
        source="stock_item.name", read_only=True, default=None,
    )
    location_code = serializers.CharField(
        source="location.code", read_only=True, default=None,
    )
    unit_cost = serializers.IntegerField(read_only=True)

    class Meta:
        model = StockBalance
        fields = [
            "id", "stock_item_id", "stock_item_code", "stock_item_name",
            "location_id", "location_code",
            "on_hand_qty", "stock_value", "unit_cost", "updated_at",
        ]


class StockMovementSerializer(serializers.ModelSerializer):
    """One immutable stock-ledger event with signed and running-balance values.

    Raw integer values remain the accounting contract; ``*_naira`` fields are display
    mirrors. The surrounding detail queryset selects ``created_by`` and ``stock_item``.
    """

    stock_item_code = serializers.CharField(
        source="stock_item.code", read_only=True, default=None,
    )
    location_code = serializers.CharField(
        source="location.code", read_only=True, default=None,
    )
    created_by_name = serializers.SerializerMethodField()
    value_amount_naira = serializers.SerializerMethodField()
    balance_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id", "stock_item_id", "stock_item_code",
            "location_id", "location_code", "movement_type",
            "movement_date", "quantity", "value_amount", "value_amount_naira",
            "balance_qty", "balance_value", "balance_value_naira",
            "grn_id", "journal_id", "reference", "narration",
            "created_by_name", "created_at",
        ]

    def get_created_by_name(self, obj) -> str:
        # The actor who recorded the movement, else "System" (GRN receipts may be unattributed).
        user = obj.created_by
        if user is None:
            return "System"
        return (
            f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
            or getattr(user, "email", "System")
        )

    def get_value_amount_naira(self, obj) -> str:
        return format_naira(obj.value_amount)

    def get_balance_value_naira(self, obj) -> str:
        return format_naira(obj.balance_value)


# --------------------------------------------------------------------------- #
# Purchase requisition                                                        #
# --------------------------------------------------------------------------- #

class RequisitionLineSerializer(serializers.ModelSerializer):
    """Read-only requisition estimate line, including its derived kobo extension."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)
    estimated_line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = PurchaseRequisitionLine
        fields = [
            "id", "line_no", "catalog_item_id", "description", "quantity", "unit", "estimated_unit_price",
            "expense_account_id", "expense_code", "tax_code_id", "estimated_line_total",
        ]


class RequisitionSerializer(serializers.ModelSerializer):
    """Requisition header and lines with document and workflow states kept separate.

    ``status`` is the shared finance-document lifecycle while ``approval_state`` is the
    spend-workflow overlay. Clients receive both and must not infer one from the other.
    ``is_parked`` is a third, derived signal: PENDING but nobody can act on it.
    ``approved_by_override`` is a fourth: it was released without a vote, by a named
    human holding ``procurement.approval.override``. An APPROVED requisition carrying
    this flag was never reviewed, which is exactly what makes it worth showing.
    """

    lines = RequisitionLineSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    estimated_total_naira = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    is_parked = serializers.SerializerMethodField()
    approved_by_override = serializers.SerializerMethodField()
    cost_center_code = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    cost_center_name = serializers.CharField(source="cost_center.name", read_only=True, default=None)

    class Meta:
        model = PurchaseRequisition
        fields = [
            "id", "document_number", "status", "approval_state", "is_parked",
            "approved_by_override", "branch_id", "branch_name", "title",
            "request_date", "needed_by", "requested_by_id", "requested_by_name",
            "cost_center_id", "cost_center_code", "cost_center_name",
            "justification", "estimated_total", "estimated_total_naira", "created_at", "lines",
        ]

    def get_estimated_total_naira(self, obj) -> str:
        return format_naira(obj.estimated_total)

    def get_is_parked(self, obj) -> bool:
        """Submitted, but its approval stage has nobody eligible to decide it.

        List endpoints resolve the whole page once and pass the answer in through
        ``parked_requisition_ids`` so this stays free of per-row queries. Single-document
        responses (create, patch, submit) have no such context and fall back to the
        one-document check, which short-circuits without a query unless the document is
        actually PENDING approval.
        """
        parked_ids = self.context.get("parked_requisition_ids")
        if parked_ids is None:
            from .approval_parking import is_document_parked

            return is_document_parked(obj)
        return obj.pk in parked_ids

    def get_approved_by_override(self, obj) -> bool:
        """Released without review by a permissioned human, rather than decided.

        Resolved for the whole page at once through ``overridden_requisition_ids``, the
        same shape ``is_parked`` uses; a single-document response falls back to one
        indexed existence check.
        """
        overridden_ids = self.context.get("overridden_requisition_ids")
        if overridden_ids is None:
            from .approval_override import is_document_overridden

            return is_document_overridden(obj)
        return obj.pk in overridden_ids

    def get_requested_by_name(self, obj) -> str:
        user = obj.requested_by
        if user is None:
            return "System"
        # Prefer the platform display name, then Django's composed name, then the stable email identifier.
        return getattr(user, "full_name", "") or user.get_full_name() or user.email


# --------------------------------------------------------------------------- #
# Sourcing - shared helpers                                                   #
# --------------------------------------------------------------------------- #

def _sourcing_activity(entity, target_type, target_id):
    """Finance-audit activity feed for one sourcing document.

    Same shape/pattern as the vendor-invoice / vendor-payment drawers: the immutable
    :class:`FinanceAuditLog` rows for this exact document, newest first, capped. Only
    called on single-record detail reads, so the bounded query is not an N+1 concern.
    """
    from vs_finance.models import FinanceAuditLog

    return [{
        "id": log.id, "action": log.action, "message": log.message, "status": log.status,
        "actor_name": (
            f"{getattr(log.actor, 'first_name', '')} {getattr(log.actor, 'last_name', '')}".strip()
            or getattr(log.actor, "email", "System")
        ) if log.actor_id else "System",
        "created_at": log.created_at,
    } for log in FinanceAuditLog.objects.filter(
        entity=entity, target_type=target_type, target_id=str(target_id),
    ).select_related("actor").order_by("-created_at")[:20]]


def _quotation_is_expired(quotation) -> bool:
    """Display-only overlay: a SUBMITTED quote whose validity date has passed.

    Never persisted (there is no expiry scheduler); computed against today so the list
    and drawer can flag a lapsed bid without rewriting its authoritative status.
    """
    from .constants import QuotationStatus

    return bool(
        quotation.quotation_status == QuotationStatus.SUBMITTED
        and quotation.valid_until is not None
        and quotation.valid_until < timezone.localdate()
    )


# --------------------------------------------------------------------------- #
# Request for quotation (sourcing)                                            #
# --------------------------------------------------------------------------- #

class RfqLineSerializer(serializers.ModelSerializer):
    """RFQ specification line; pricing arrives later on vendor quotation lines."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)

    class Meta:
        model = RfqLine
        fields = [
            "id", "line_no", "description", "quantity",
            "requisition_line_id", "expense_account_id", "expense_code", "tax_code_id",
            "version", "is_active",
        ]


class RfqListSerializer(serializers.ModelSerializer):
    """Lean list row. ``line_count``/``response_count`` are queryset annotations
    (see :func:`RfqListCreateView.get`) - never per-row counts, so the list stays O(1)."""

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    requisition_number = serializers.CharField(
        source="requisition.document_number", read_only=True, default=None,
    )
    line_count = serializers.IntegerField(read_only=True)
    response_count = serializers.IntegerField(read_only=True)
    invited_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = RequestForQuotation
        fields = [
            "id", "document_number", "rfq_status", "title",
            "branch_id", "branch_name",
            "requisition_id", "requisition_number", "issue_date", "response_due_date",
            "response_due_at", "version", "budget_estimate", "line_count", "response_count", "invited_count",
        ]


class RfqQuotationSummarySerializer(serializers.ModelSerializer):
    """A received quotation as it appears in the RFQ drawer's Quotations tab."""

    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = VendorQuotation
        fields = [
            "id", "document_number", "vendor_code", "vendor_name",
            "quotation_status", "total", "lead_time_days",
            "quote_date", "valid_until", "is_expired",
        ]

    def get_is_expired(self, obj) -> bool:
        return _quotation_is_expired(obj)


class RfqDetailSerializer(serializers.ModelSerializer):
    """Full RFQ record for the detail drawer: lines, received quotations, activity."""

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    requisition_number = serializers.CharField(
        source="requisition.document_number", read_only=True, default=None,
    )
    line_count = serializers.SerializerMethodField()
    response_count = serializers.SerializerMethodField()
    invited_count = serializers.SerializerMethodField()
    lines = RfqLineSerializer(many=True, read_only=True)
    invitations = serializers.SerializerMethodField()
    quotations = serializers.SerializerMethodField()
    activity = serializers.SerializerMethodField()

    class Meta:
        model = RequestForQuotation
        fields = [
            "id", "document_number", "rfq_status", "title",
            "branch_id", "branch_name",
            "requisition_id", "requisition_number", "issue_date", "response_due_date",
            "response_due_at", "version", "budget_estimate", "notes", "line_count", "response_count", "invited_count",
            "lines", "invitations", "quotations", "amendments", "activity",
        ]

    amendments = serializers.SerializerMethodField()

    def get_line_count(self, obj) -> int:
        return len(obj.lines.all())

    def get_response_count(self, obj) -> int:
        from .constants import QuotationStatus

        # A "response" is any quotation a vendor has actually submitted (not a draft).
        return sum(
            1 for q in obj.quotations.all()
            if q.quotation_status != QuotationStatus.DRAFT
        )

    def get_invited_count(self, obj) -> int:
        return len(obj.invitations.all())

    def get_invitations(self, obj):
        # Derive each invitation's "responded" flag by joining the invited vendors to
        # this RFQ's newest-first quotations in Python (both prefetched) - never a
        # per-row query. Multiple quotations are allowed; the newest is canonical.
        quote_by_vendor = {}
        for q in obj.quotations.all():
            # The view orders created_at DESC, id DESC, so first is deterministically newest.
            quote_by_vendor.setdefault(q.vendor_id, q)
        request = self.context.get("request")
        can_view_contacts = bool(request and (
            is_vision_super_admin(request.user)
            or user_has_rbac_permission(
                request.user, "procurement.vendor.view_sensitive",
                tenant=getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None),
            )
        ))
        rows = []
        for inv in obj.invitations.all():
            quote = quote_by_vendor.get(inv.vendor_id)
            visible_quote = (
                quote if quote and not (
                    quote.vendor_managed and quote.quotation_status == QuotationStatus.DRAFT
                ) else None
            )
            rows.append({
                "id": inv.id,
                "vendor_id": inv.vendor_id,
                "vendor_code": inv.vendor.code,
                "vendor_name": inv.vendor.name,
                "responded": visible_quote is not None,
                "quotation_id": visible_quote.id if visible_quote else None,
                "quotation_status": visible_quote.quotation_status if visible_quote else None,
                "quotation_total": visible_quote.total if visible_quote else None,
                "status": inv.status,
                "deadline": inv.deadline,
                "opened_at": inv.opened_at,
                "draft_started_at": inv.draft_started_at,
                "submitted_at": inv.submitted_at,
                "declined_at": inv.declined_at,
                "decline_reason": inv.decline_reason,
                "recipients": [
                    {"name": row.name, "email": row.email} for row in inv.recipients.all()
                ] if can_view_contacts else [],
            })
        return rows

    def get_amendments(self, obj):
        return [
            {"id": row.id, "version": row.version, "summary": row.summary,
             "response_required": row.response_required, "published_at": row.published_at}
            for row in obj.amendments.all()
        ]

    def get_quotations(self, obj):
        # Reuse the view's explicit newest-first prefetch cache; ordering here would re-query.
        return RfqQuotationSummarySerializer(obj.quotations.all(), many=True).data

    def get_activity(self, obj):
        return _sourcing_activity(obj.entity_id, "RequestForQuotation", obj.pk)


# --------------------------------------------------------------------------- #
# Vendor quotation (sourcing)                                                 #
# --------------------------------------------------------------------------- #

class VendorQuotationLineSerializer(serializers.ModelSerializer):
    """Priced vendor response line with integer-kobo net and tax amounts."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)

    class Meta:
        model = VendorQuotationLine
        fields = [
            "id", "line_no", "description", "rfq_line_id",
            "expense_account_id", "expense_code",
            "quantity", "unit_price", "tax_code_id", "net_amount", "tax_amount",
            "response_type", "alternative_for_id",
        ]


class QuotationListSerializer(serializers.ModelSerializer):
    """Lean quotation list row. ``is_expired`` is a display overlay, never persisted."""

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    rfq_number = serializers.CharField(source="rfq.document_number", read_only=True)
    total_naira = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = VendorQuotation
        fields = [
            "id", "document_number", "quotation_status",
            "branch_id", "branch_name",
            "rfq_id", "rfq_number", "vendor_id", "vendor_code", "vendor_name",
            "quote_date", "valid_until", "lead_time_days", "reference",
            "total", "total_naira", "is_expired", "awarded_po_id",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_is_expired(self, obj) -> bool:
        return _quotation_is_expired(obj)


class QuotationDetailSerializer(serializers.ModelSerializer):
    """Full quotation record for the detail drawer: lines, totals, PO link, activity."""

    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    rfq_number = serializers.CharField(source="rfq.document_number", read_only=True)
    awarded_po_number = serializers.CharField(
        source="awarded_po.document_number", read_only=True, default=None,
    )
    total_naira = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    lines = VendorQuotationLineSerializer(many=True, read_only=True)
    activity = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    submissions = serializers.SerializerMethodField()

    class Meta:
        model = VendorQuotation
        fields = [
            "id", "document_number", "quotation_status",
            "branch_id", "branch_name",
            "rfq_id", "rfq_number", "vendor_id", "vendor_code", "vendor_name",
            "quote_date", "valid_until", "currency_id", "lead_time_days",
            "reference", "notes", "is_expired",
            "subtotal", "tax_total", "total", "total_naira",
            "awarded_po_id", "awarded_po_number", "lines", "attachments", "submissions", "activity",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_is_expired(self, obj) -> bool:
        return _quotation_is_expired(obj)

    def get_activity(self, obj):
        return _sourcing_activity(obj.entity_id, "VendorQuotation", obj.pk)

    def get_attachments(self, obj):
        return [
            {"id": row.id, "name": row.original_name, "content_type": row.content_type,
             "size": row.size, "revision": row.revision, "url": row.file.url}
            for row in obj.attachments.all()
        ]

    def get_submissions(self, obj):
        return [
            {"id": row.id, "revision": row.revision, "rfq_version": row.rfq_version,
             "submitted_at": row.submitted_at, "submitted_by_email": row.submitted_by_email}
            for row in obj.submissions.all()
        ]


# --------------------------------------------------------------------------- #
# Sourcing - external vendor portal read model                                #
# --------------------------------------------------------------------------- #

class VendorPortalQuotationLineSerializer(serializers.ModelSerializer):
    """A vendor's own answer line, without the buyer's GL coding.

    ``expense_account`` and ``tax_code`` are copied onto quotation lines from the RFQ
    so the buyer can post an award, but they are internal chart-of-accounts data and
    never belong in a response sent outside the organisation.
    """

    class Meta:
        model = VendorQuotationLine
        fields = [
            "id", "line_no", "rfq_line_id", "description", "quantity",
            "unit_price", "net_amount", "tax_amount", "response_type",
        ]


class VendorPortalQuotationSerializer(serializers.ModelSerializer):
    """Quotation shape returned to the external vendor portal.

    Deliberately not :class:`QuotationDetailSerializer`: that buyer read model carries
    the internal finance-audit feed (buyer staff names and award/rejection wording),
    expense-account coding, and ``/media/`` attachment URLs that only an authenticated
    console user can fetch. This serializer is an allow-list of the vendor's own
    submission content, so widening the buyer shape can never leak outward by default.

    Attachments are intentionally absent: the portal payload lists them separately and
    they are only retrievable through the session-gated public download route.
    """

    lines = VendorPortalQuotationLineSerializer(many=True, read_only=True)

    class Meta:
        model = VendorQuotation
        fields = [
            "id", "document_number", "quotation_status",
            "quote_date", "valid_until", "currency_id", "lead_time_days",
            "reference", "notes", "subtotal", "tax_total", "total", "lines",
        ]


# --------------------------------------------------------------------------- #
# Purchase order                                                              #
# --------------------------------------------------------------------------- #

class POLineSerializer(serializers.ModelSerializer):
    """PO line including service-owned receipt and invoice progress counters."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True)

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "id", "line_no", "description", "expense_account_id", "expense_code",
            "quantity", "unit_price", "tax_code_id",
            "net_amount", "tax_amount", "received_qty", "invoiced_qty",
        ]


class POReceiptDocumentSerializer(serializers.ModelSerializer):
    """Compact GRN link embedded in a PO detail response."""

    item_count = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceivedNote
        fields = ["id", "document_number", "received_date", "status", "item_count"]

    def get_item_count(self, obj) -> int:
        # Detail queries prefetch receipt lines; len therefore avoids a count query per receipt.
        return len(obj.lines.all())


class POInvoiceDocumentSerializer(serializers.ModelSerializer):
    """Compact vendor-invoice link embedded in a PO detail response."""

    class Meta:
        model = VendorInvoice
        fields = ["id", "document_number", "invoice_date", "total", "status", "match_status"]


class PurchaseOrderVendorDeliverySerializer(serializers.ModelSerializer):
    """Operational history without exposing recipient addresses through ordinary PO reads."""

    requested_by_name = serializers.SerializerMethodField()
    recipient_count = serializers.SerializerMethodField()
    cc_count = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrderVendorDelivery
        fields = [
            "id", "source", "status", "requested_by_name", "recipient_count", "cc_count",
            "buyer_message", "queued_at", "sent_at", "cancelled_at", "failure_reason",
            "created_at", "parent_id",
        ]

    def get_requested_by_name(self, obj):
        user = obj.requested_by
        return (getattr(user, "full_name", "") or user.email) if user else "System"

    def get_recipient_count(self, obj):
        return len(obj.recipients or [])

    def get_cc_count(self, obj):
        return len(obj.cc or [])


class PurchaseOrderSerializer(serializers.ModelSerializer):
    """Full PO read model: commercial totals, progress, source, and child documents.

    The API deliberately exposes inherited ``status``, workflow ``approval_state``, and
    the UI-only ``display_status`` together. Receipt/invoice arrays and progress methods
    rely on the detail view's prefetched relations; list responses use the subclass below.
    """

    lines = POLineSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    requisition_number = serializers.CharField(
        source="requisition.document_number", read_only=True, default=None,
    )
    contract_reference = serializers.CharField(
        source="contract.reference", read_only=True, default=None,
    )
    total_naira = serializers.SerializerMethodField()
    received_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    invoiced_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    display_status = serializers.SerializerMethodField()
    quotation_number = serializers.SerializerMethodField()
    receipt_documents = POReceiptDocumentSerializer(source="goods_receipts", many=True, read_only=True)
    invoice_documents = POInvoiceDocumentSerializer(source="vendor_invoices", many=True, read_only=True)
    email_deliveries = PurchaseOrderVendorDeliverySerializer(
        source="vendor_deliveries", many=True, read_only=True,
    )
    can_email_vendor = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "document_number", "status", "approval_state", "display_status",
            "branch_id", "branch_name",
            "vendor_id", "vendor_code", "vendor_name", "requisition_id", "requisition_number",
            "contract_id", "contract_reference",
            "quotation_number", "order_date", "expected_date", "delivery_address",
            "payment_terms", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "received_pct", "invoiced_pct", "lines", "receipt_documents", "invoice_documents",
            "email_deliveries", "can_email_vendor",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_display_status(self, obj) -> str:
        # Approval overlays take precedence; a partially received draft must not look issued.
        if obj.status == DocumentStatus.DRAFT:
            return DocumentStatus.DRAFT
        if obj.status == DocumentStatus.PENDING_APPROVAL or obj.approval_state == ProcApprovalState.PENDING:
            return DocumentStatus.PENDING_APPROVAL
        stage = po_receipt_stage(
            sum((line.quantity for line in obj.lines.all()), 0),
            sum((line.received_qty for line in obj.lines.all()), 0),
        )
        # The list distinguishes work in progress; fully received POs retain their approved document state.
        return "PARTIAL" if stage == "PARTIAL" else obj.status

    def get_quotation_number(self, obj) -> str | None:
        # Awarded quotations point back to their PO through a reverse relation, not a PO foreign key.
        quotation = next(iter(obj.source_quotation.all()), None)
        return quotation.document_number if quotation else None

    def get_can_email_vendor(self, obj) -> bool:
        return (
            obj.status == DocumentStatus.APPROVED
            and obj.approval_state == ProcApprovalState.APPROVED
        )


class PurchaseOrderListSerializer(PurchaseOrderSerializer):
    """Lighter list row: the nested line/receipt/invoice documents belong to the
    detail drawer (its own request), so the list never ships or prefetches them.
    ``display_status``/``received_pct``/``invoiced_pct`` are still computed from the
    prefetched lines - only the serialised line array is dropped."""

    lines = None
    receipt_documents = None
    invoice_documents = None
    email_deliveries = None

    class Meta(PurchaseOrderSerializer.Meta):
        fields = [
            f for f in PurchaseOrderSerializer.Meta.fields
            if f not in ("lines", "receipt_documents", "invoice_documents", "email_deliveries")
        ]


# --------------------------------------------------------------------------- #
# Goods received note                                                         #
# --------------------------------------------------------------------------- #

class GRNLineSerializer(serializers.ModelSerializer):
    """Receipt line with a compatibility description for pre-snapshot rows."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True)
    cost_center_code = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    stock_item_code = serializers.CharField(
        source="stock_item.code", read_only=True, default=None,
    )
    stock_item_name = serializers.CharField(
        source="stock_item.name", read_only=True, default=None,
    )
    description = serializers.SerializerMethodField()

    def get_description(self, obj):
        # Older receipts may have a blank copied description, so resolve the source PO item for display.
        return obj.description or (obj.po_line.description if obj.po_line_id else "")

    class Meta:
        model = GoodsReceivedNoteLine
        fields = [
            "id", "line_no", "po_line_id", "description",
            "expense_account_id", "expense_code",
            "cost_center_id", "cost_center_code",
            "stock_item_id", "stock_item_code", "stock_item_name",
            "accepted_qty", "rejected_qty", "expected_qty", "unit_price", "value_amount",
        ]


class GoodsReceivedNoteSerializer(serializers.ModelSerializer):
    """GRN header/lines plus a delivery-quality overlay independent of posting status.

    ``status`` says whether the accounting document posted. ``receipt_status`` compares
    accepted/rejected quantities with the receipt-time expectation and therefore answers
    a different operational question.
    """

    lines = GRNLineSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    purchase_order_number = serializers.CharField(source="purchase_order.document_number", read_only=True, default=None)
    received_by_name = serializers.SerializerMethodField()
    receipt_status = serializers.SerializerMethodField()
    received_item_count = serializers.SerializerMethodField()
    ordered_item_count = serializers.SerializerMethodField()
    purchase_order_fulfilment_status = serializers.SerializerMethodField()
    purchase_order_received_item_count = serializers.SerializerMethodField()
    purchase_order_ordered_item_count = serializers.SerializerMethodField()
    purchase_order_remaining_item_count = serializers.SerializerMethodField()
    total_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceivedNote
        fields = [
            "id", "document_number", "status", "receipt_status", "vendor_id", "vendor_code", "vendor_name", "received_by_name",
            "branch_id", "branch_name",
            "purchase_order_id", "purchase_order_number", "received_date", "reference", "narration",
            "received_item_count", "ordered_item_count",
            "purchase_order_fulfilment_status", "purchase_order_received_item_count",
            "purchase_order_ordered_item_count", "purchase_order_remaining_item_count",
            "total_value", "total_value_naira", "journal_id", "lines",
        ]

    def get_total_value_naira(self, obj) -> str:
        return format_naira(obj.total_value)

    def get_received_by_name(self, obj) -> str:
        user = obj.received_by
        if user is None:
            return "System"
        return getattr(user, "full_name", "") or user.get_full_name() or user.email

    def _expected_quantity(self, obj):
        """Resolve this receipt's expected quantity, including legacy fallbacks.

        New rows persist ``expected_qty`` per line. Older/directly-created fixtures may
        not, so the original PO quantity is the next-best source, followed by the receipt
        itself. The object-local cache keeps three serializer fields on one query path.
        """
        cached = getattr(obj, "_receipt_expected_quantity", None)
        if cached is not None:
            return cached
        # Snapshot totals preserve the PO remainder that this specific receipt was raised against.
        expected = sum((line.expected_qty for line in obj.lines.all()), 0)
        if not expected and obj.purchase_order_id:
            # Legacy/directly-created rows without a snapshot fall back to the original PO total.
            expected = sum((line.quantity for line in obj.purchase_order.lines.all()), 0)
        if not expected:
            expected = sum((line.accepted_qty + line.rejected_qty for line in obj.lines.all()), 0)
        obj._receipt_expected_quantity = expected
        return expected

    def get_receipt_status(self, obj) -> str:
        # Quality/receipt state compares this delivery with its expected PO quantity, not the GL posting status.
        accepted = sum((line.accepted_qty for line in obj.lines.all()), 0)
        rejected = sum((line.rejected_qty for line in obj.lines.all()), 0)
        if rejected and not accepted:
            return "REJECTED"
        expected = self._expected_quantity(obj)
        return "PARTIAL" if rejected or accepted < expected else "FULL"

    def get_received_item_count(self, obj) -> str:
        return str(sum((line.accepted_qty for line in obj.lines.all()), 0))

    def get_ordered_item_count(self, obj) -> str:
        return str(self._expected_quantity(obj))

    def _purchase_order_fulfilment(self, obj):
        """Return current PO-wide accepted progress without rewriting GRN history."""
        cached = getattr(obj, "_purchase_order_fulfilment", None)
        if cached is not None:
            return cached
        if not obj.purchase_order_id:
            return None
        lines = obj.purchase_order.lines.all()
        ordered = sum((line.quantity for line in lines), 0)
        received = sum((line.received_qty for line in lines), 0)
        remaining = sum((max(line.quantity - line.received_qty, 0) for line in lines), 0)
        value = {
            "status": po_receipt_stage(ordered, received),
            "received": received,
            "ordered": ordered,
            "remaining": remaining,
        }
        obj._purchase_order_fulfilment = value
        return value

    def get_purchase_order_fulfilment_status(self, obj) -> str | None:
        progress = self._purchase_order_fulfilment(obj)
        return progress["status"] if progress else None

    def get_purchase_order_received_item_count(self, obj) -> str | None:
        progress = self._purchase_order_fulfilment(obj)
        return str(progress["received"]) if progress else None

    def get_purchase_order_ordered_item_count(self, obj) -> str | None:
        progress = self._purchase_order_fulfilment(obj)
        return str(progress["ordered"]) if progress else None

    def get_purchase_order_remaining_item_count(self, obj) -> str | None:
        progress = self._purchase_order_fulfilment(obj)
        return str(progress["remaining"]) if progress else None


class GoodsReceivedNoteListSerializer(GoodsReceivedNoteSerializer):
    """Lighter list row: the receipt lines belong to the detail drawer (its own
    request). ``receipt_status`` and the item counts are still computed from the
    prefetched lines - only the serialised line array is dropped from the payload."""

    lines = None

    class Meta(GoodsReceivedNoteSerializer.Meta):
        fields = [f for f in GoodsReceivedNoteSerializer.Meta.fields if f != "lines"]


# --------------------------------------------------------------------------- #
# Vendor invoice                                                              #
# --------------------------------------------------------------------------- #

class VendorInvoiceLineSerializer(serializers.ModelSerializer):
    """Billed line with optional PO/GRN provenance used by three-way matching."""

    expense_code = serializers.CharField(source="expense_account.code", read_only=True)
    cost_center_code = serializers.CharField(source="cost_center.code", read_only=True, default=None)

    class Meta:
        model = VendorInvoiceLine
        fields = [
            "id", "line_no", "po_line_id", "grn_line_id", "description",
            "expense_account_id", "expense_code",
            "cost_center_id", "cost_center_code",
            "quantity", "unit_price", "tax_code_id", "net_amount", "tax_amount",
        ]


class VendorInvoiceSerializer(serializers.ModelSerializer):
    """AP invoice read model spanning posting, approval, match, and settlement states.

    Each lifecycle remains available as an authoritative field. ``display_status`` is
    only a precedence-based UI overlay and never mutates or replaces those source states.
    ``amount_paid``/``balance_due`` reflect posted allocation authority.
    """

    lines = VendorInvoiceLineSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    purchase_order_number = serializers.CharField(
        source="purchase_order.document_number", read_only=True, default=None,
    )
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    display_status = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = VendorInvoice
        fields = [
            "id", "document_number", "status", "approval_state", "match_status", "payment_status",
            "branch_id", "branch_name",
            "display_status", "is_overdue",
            "vendor_id", "vendor_code", "vendor_name", "purchase_order_id", "purchase_order_number",
            "invoice_date", "due_date", "vendor_reference", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "journal_id", "lines", "attachments",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

    def get_attachments(self, obj):
        from .attachments import serialize_attachments

        return serialize_attachments(obj)

    def get_is_overdue(self, obj) -> bool:
        # Overdue is a date/payment overlay, never a replacement for POSTED ledger status.
        return bool(
            obj.status == DocumentStatus.POSTED and obj.due_date
            and obj.due_date < timezone.localdate() and obj.balance_due > 0
        )

    def get_display_status(self, obj) -> str:
        # The list's single headline status follows the most actionable overlay;
        # callers still receive every underlying lifecycle field separately.
        if obj.payment_status == "PAID":
            return "PAID"
        if obj.payment_status == "PARTIAL":
            return "PARTIAL"
        if self.get_is_overdue(obj):
            return "OVERDUE"
        if obj.match_status in ("UNDER_RECEIVED", "OVER_BILLED"):
            return "DISPUTED"
        if obj.approval_state == ProcApprovalState.PENDING:
            return "PENDING_APPROVAL"
        if obj.approval_state in (ProcApprovalState.APPROVED, ProcApprovalState.REJECTED):
            return obj.approval_state
        return obj.status


class VendorInvoiceListSerializer(VendorInvoiceSerializer):
    """List rows omit line and attachment arrays; the detail drawer fetches those."""

    lines = None
    attachments = None

    class Meta(VendorInvoiceSerializer.Meta):
        fields = [
            f for f in VendorInvoiceSerializer.Meta.fields
            if f not in ("lines", "attachments")
        ]


# --------------------------------------------------------------------------- #
# Vendor payment                                                              #
# --------------------------------------------------------------------------- #

class VendorPaymentAllocationSerializer(serializers.ModelSerializer):
    """A payment split enriched with the target invoice's current settlement snapshot.

    The allocation ``amount`` is gross AP settled; it is intentionally not the payment's
    net bank outflow when withholding tax is present.
    """

    invoice_number = serializers.CharField(
        source="vendor_invoice.document_number", read_only=True,
    )
    invoice_date = serializers.DateField(source="vendor_invoice.invoice_date", read_only=True)
    due_date = serializers.DateField(source="vendor_invoice.due_date", read_only=True)
    invoice_total = serializers.IntegerField(source="vendor_invoice.total", read_only=True)
    invoice_balance = serializers.IntegerField(source="vendor_invoice.balance_due", read_only=True)
    invoice_payment_status = serializers.CharField(source="vendor_invoice.payment_status", read_only=True)

    class Meta:
        model = VendorPaymentAllocation
        fields = [
            "id", "vendor_invoice_id", "invoice_number", "invoice_date", "due_date",
            "invoice_total", "invoice_balance", "invoice_payment_status", "amount",
        ]


class VendorPaymentSerializer(serializers.ModelSerializer):
    """Vendor disbursement with approval, posting, allocation, and bank context.

    Draft allocation rows express the intended split. Once POSTED, the header's
    ``allocated_amount`` is authoritative because the service updates it only after the
    journal succeeds; :meth:`get_allocation_status` preserves that distinction.
    """

    allocations = VendorPaymentAllocationSerializer(many=True, read_only=True)
    branch_name = serializers.CharField(
        source="branch.name", read_only=True, default=None,
    )
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    payment_code = serializers.CharField(source="payment_account.code", read_only=True, default=None)
    payment_account_name = serializers.CharField(source="payment_account.name", read_only=True, default=None)
    bank_account_id = serializers.IntegerField(source="payment_account.bank_account.id", read_only=True, default=None)
    bank_account_name = serializers.CharField(source="payment_account.bank_account.name", read_only=True, default=None)
    wht_tax_code_value = serializers.CharField(source="wht_tax_code.code", read_only=True, default=None)
    created_by_name = serializers.SerializerMethodField()
    unallocated_amount = serializers.IntegerField(read_only=True)
    # The same kobo named for where they actually sit: unallocated gross was never
    # debited to AP, it is a vendor advance in 1240 until a bill draws it down.
    advance_remaining = serializers.IntegerField(read_only=True)
    allocation_status = serializers.SerializerMethodField()
    net_naira = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = VendorPayment
        fields = [
            "id", "document_number", "status", "approval_state", "allocation_status",
            "branch_id", "branch_name",
            "vendor_id", "vendor_code", "vendor_name",
            "payment_date", "method",
            "gross_amount", "wht_amount", "net_amount", "net_naira",
            "allocated_amount", "unallocated_amount", "advance_remaining",
            "payment_account_id", "payment_code",
            "payment_account_name", "bank_account_id", "bank_account_name",
            "wht_tax_code_id", "wht_tax_code_value", "reference", "narration",
            "journal_id", "created_at", "created_by_name", "allocations", "attachments",
        ]

    def get_net_naira(self, obj) -> str:
        return format_naira(obj.net_amount)

    def get_attachments(self, obj):
        from .attachments import serialize_attachments

        return serialize_attachments(obj)

    def get_created_by_name(self, obj) -> str:
        if not obj.created_by_id:
            return "System"
        return (
            f"{getattr(obj.created_by, 'first_name', '')} {getattr(obj.created_by, 'last_name', '')}".strip()
            or getattr(obj.created_by, "email", "System")
        )

    def get_allocation_status(self, obj) -> str:
        # Draft rows are a planned split; posted rows use the authoritative amount
        # advanced by the allocation service after the journal succeeds.
        amount = obj.allocated_amount if obj.status == DocumentStatus.POSTED else sum(
            allocation.amount for allocation in obj.allocations.all()
        )
        if amount <= 0:
            return "UNALLOCATED"
        if amount < obj.gross_amount:
            return "PARTIAL"
        return "FULL"


class VendorPaymentListSerializer(VendorPaymentSerializer):
    """List contract retains invoice references but omits detail-only activity/GL data.

    Attachments are dropped here rather than merely left unprefetched: the list source
    (``_payment_list_queryset``) does not prefetch them, so serializing the field would
    cost one query per row.
    """

    attachments = None

    class Meta(VendorPaymentSerializer.Meta):
        fields = [f for f in VendorPaymentSerializer.Meta.fields if f != "attachments"]
