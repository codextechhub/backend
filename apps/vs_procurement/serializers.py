"""DRF serializers for the vs_procurement REST API (the Procure-to-Pay surface).

Read-side serialisers for the AP sub-ledger and the purchasing chain
(PR → PO → GRN → vendor invoice → vendor payment). Document *creation* is handled in
the views (they parse the request, resolve GL accounts by code/id and call the
purchasing/payables services), so these serialisers stay read-only — mirroring how
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

from .constants import ProcApprovalState
from .purchasing import po_receipt_stage
from .models import (
    CatalogItem,
    ContractMilestone,
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
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
    VendorPaymentAllocation,
    VendorQuotation,
    VendorQuotationLine,
)


# --------------------------------------------------------------------------- #
# Master data                                                                 #
# --------------------------------------------------------------------------- #

class VendorCategorySerializer(serializers.ModelSerializer):
    default_expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )

    class Meta:
        model = VendorCategory
        fields = [
            "id", "code", "name", "default_expense_account_id",
            "default_expense_code", "is_active",
        ]


class VendorSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    payable_code = serializers.CharField(source="payable_account.code", read_only=True, default=None)
    default_expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )

    # FLS: vendor banking details are PII used for disbursement — only holders of
    # the sensitive grant see them; everyone else gets the record with these
    # fields stripped.
    read_permissions = {
        "bank_name": "procurement.vendor.view_sensitive",
        "bank_account_number": "procurement.vendor.view_sensitive",
        "bank_account_name": "procurement.vendor.view_sensitive",
    }

    class Meta:
        model = Vendor
        fields = [
            "id", "code", "name", "category_id", "category_code",
            "email", "phone", "tax_id",
            "bank_name", "bank_account_number", "bank_account_name",
            "payable_account_id", "payable_code",
            "default_expense_account_id", "default_expense_code",
            "payment_terms", "kyc_status", "risk", "on_hold", "is_active",
        ]


# --------------------------------------------------------------------------- #
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

class ContractMilestoneSerializer(serializers.ModelSerializer):
    amount_naira = serializers.SerializerMethodField()

    class Meta:
        model = ContractMilestone
        fields = [
            "id", "line_no", "name", "due_date", "amount", "amount_naira",
            "status", "completed_date", "note",
        ]

    def get_amount_naira(self, obj) -> str:
        return format_naira(obj.amount)


class VendorContractSerializer(serializers.ModelSerializer):
    milestones = ContractMilestoneSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    renewal_window_start = serializers.DateField(read_only=True)
    contract_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = VendorContract
        fields = [
            "id", "reference", "title", "status",
            "vendor_id", "vendor_code",
            "start_date", "end_date", "renewal_window_start",
            "contract_value", "contract_value_naira", "payment_terms",
            "auto_renew", "renewal_notice_days", "renews_id", "notes",
            "milestones",
        ]

    def get_contract_value_naira(self, obj) -> str:
        return format_naira(obj.contract_value)


# --------------------------------------------------------------------------- #
# Item catalog                                                                #
# --------------------------------------------------------------------------- #

class CatalogItemSerializer(serializers.ModelSerializer):
    preferred_vendor_code = serializers.CharField(
        source="preferred_vendor.code", read_only=True, default=None,
    )
    expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )
    tax_code = serializers.CharField(
        source="default_tax_code.code", read_only=True, default=None,
    )
    standard_unit_price_naira = serializers.SerializerMethodField()

    class Meta:
        model = CatalogItem
        fields = [
            "id", "code", "name", "description", "unit_of_measure",
            "preferred_vendor_id", "preferred_vendor_code",
            "default_expense_account_id", "expense_code",
            "default_tax_code_id", "tax_code",
            "lead_time_days", "standard_unit_price", "standard_unit_price_naira",
            "is_active",
        ]

    def get_standard_unit_price_naira(self, obj) -> str:
        return format_naira(obj.standard_unit_price)


# --------------------------------------------------------------------------- #
# Inventory / stock ledger                                                    #
# --------------------------------------------------------------------------- #

class StockItemSerializer(serializers.ModelSerializer):
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


class StockMovementSerializer(serializers.ModelSerializer):
    stock_item_code = serializers.CharField(
        source="stock_item.code", read_only=True, default=None,
    )
    value_amount_naira = serializers.SerializerMethodField()
    balance_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id", "stock_item_id", "stock_item_code", "movement_type",
            "movement_date", "quantity", "value_amount", "value_amount_naira",
            "balance_qty", "balance_value", "balance_value_naira",
            "grn_id", "journal_id", "reference", "narration", "created_at",
        ]

    def get_value_amount_naira(self, obj) -> str:
        return format_naira(obj.value_amount)

    def get_balance_value_naira(self, obj) -> str:
        return format_naira(obj.balance_value)


# --------------------------------------------------------------------------- #
# Purchase requisition                                                        #
# --------------------------------------------------------------------------- #

class RequisitionLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)
    estimated_line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = PurchaseRequisitionLine
        fields = [
            "id", "line_no", "catalog_item_id", "description", "quantity", "unit", "estimated_unit_price",
            "expense_account_id", "expense_code", "tax_code_id", "estimated_line_total",
        ]


class RequisitionSerializer(serializers.ModelSerializer):
    lines = RequisitionLineSerializer(many=True, read_only=True)
    estimated_total_naira = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    cost_center_code = serializers.CharField(source="cost_center.code", read_only=True, default=None)
    cost_center_name = serializers.CharField(source="cost_center.name", read_only=True, default=None)

    class Meta:
        model = PurchaseRequisition
        fields = [
            "id", "document_number", "status", "approval_state", "title",
            "request_date", "needed_by", "requested_by_id", "requested_by_name",
            "cost_center_id", "cost_center_code", "cost_center_name",
            "justification", "estimated_total", "estimated_total_naira", "created_at", "lines",
        ]

    def get_estimated_total_naira(self, obj) -> str:
        return format_naira(obj.estimated_total)

    def get_requested_by_name(self, obj) -> str:
        user = obj.requested_by
        if user is None:
            return "System"
        # Prefer the platform display name, then Django's composed name, then the stable email identifier.
        return getattr(user, "full_name", "") or user.get_full_name() or user.email


# --------------------------------------------------------------------------- #
# Request for quotation (sourcing)                                            #
# --------------------------------------------------------------------------- #

class RfqLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)

    class Meta:
        model = RfqLine
        fields = [
            "id", "line_no", "description", "quantity",
            "requisition_line_id", "expense_account_id", "expense_code", "tax_code_id",
        ]


class RequestForQuotationSerializer(serializers.ModelSerializer):
    lines = RfqLineSerializer(many=True, read_only=True)

    class Meta:
        model = RequestForQuotation
        fields = [
            "id", "document_number", "rfq_status", "title",
            "requisition_id", "issue_date", "response_due_date", "notes", "lines",
        ]


# --------------------------------------------------------------------------- #
# Vendor quotation (sourcing)                                                 #
# --------------------------------------------------------------------------- #

class VendorQuotationLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)

    class Meta:
        model = VendorQuotationLine
        fields = [
            "id", "line_no", "description", "rfq_line_id",
            "expense_account_id", "expense_code",
            "quantity", "unit_price", "tax_code_id", "net_amount", "tax_amount",
        ]


class VendorQuotationSerializer(serializers.ModelSerializer):
    lines = VendorQuotationLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    rfq_number = serializers.CharField(source="rfq.document_number", read_only=True)
    total_naira = serializers.SerializerMethodField()

    class Meta:
        model = VendorQuotation
        fields = [
            "id", "document_number", "quotation_status",
            "rfq_id", "rfq_number", "vendor_id", "vendor_code",
            "quote_date", "valid_until", "currency_id", "lead_time_days",
            "reference", "notes",
            "subtotal", "tax_total", "total", "total_naira",
            "awarded_po_id", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


# --------------------------------------------------------------------------- #
# Purchase order                                                              #
# --------------------------------------------------------------------------- #

class POLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True)

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "id", "line_no", "description", "expense_account_id", "expense_code",
            "quantity", "unit_price", "tax_code_id",
            "net_amount", "tax_amount", "received_qty", "invoiced_qty",
        ]


class POReceiptDocumentSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceivedNote
        fields = ["id", "document_number", "received_date", "status", "item_count"]

    def get_item_count(self, obj) -> int:
        # Detail queries prefetch receipt lines; len therefore avoids a count query per receipt.
        return len(obj.lines.all())


class POInvoiceDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorInvoice
        fields = ["id", "document_number", "invoice_date", "total", "status", "match_status"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = POLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    requisition_number = serializers.CharField(
        source="requisition.document_number", read_only=True, default=None,
    )
    total_naira = serializers.SerializerMethodField()
    received_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    invoiced_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    display_status = serializers.SerializerMethodField()
    quotation_number = serializers.SerializerMethodField()
    receipt_documents = POReceiptDocumentSerializer(source="goods_receipts", many=True, read_only=True)
    invoice_documents = POInvoiceDocumentSerializer(source="vendor_invoices", many=True, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "document_number", "status", "approval_state", "display_status",
            "vendor_id", "vendor_code", "vendor_name", "requisition_id", "requisition_number",
            "quotation_number", "order_date", "expected_date", "delivery_address",
            "payment_terms", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "received_pct", "invoiced_pct", "lines", "receipt_documents", "invoice_documents",
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


class PurchaseOrderListSerializer(PurchaseOrderSerializer):
    """Lighter list row: the nested line/receipt/invoice documents belong to the
    detail drawer (its own request), so the list never ships or prefetches them.
    ``display_status``/``received_pct``/``invoiced_pct`` are still computed from the
    prefetched lines — only the serialised line array is dropped."""

    lines = None
    receipt_documents = None
    invoice_documents = None

    class Meta(PurchaseOrderSerializer.Meta):
        fields = [
            f for f in PurchaseOrderSerializer.Meta.fields
            if f not in ("lines", "receipt_documents", "invoice_documents")
        ]


# --------------------------------------------------------------------------- #
# Goods received note                                                         #
# --------------------------------------------------------------------------- #

class GRNLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True)
    description = serializers.SerializerMethodField()

    def get_description(self, obj):
        # Older receipts may have a blank copied description, so resolve the source PO item for display.
        return obj.description or (obj.po_line.description if obj.po_line_id else "")

    class Meta:
        model = GoodsReceivedNoteLine
        fields = [
            "id", "line_no", "po_line_id", "description",
            "expense_account_id", "expense_code",
            "accepted_qty", "rejected_qty", "expected_qty", "unit_price", "value_amount",
        ]


class GoodsReceivedNoteSerializer(serializers.ModelSerializer):
    lines = GRNLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    purchase_order_number = serializers.CharField(source="purchase_order.document_number", read_only=True, default=None)
    received_by_name = serializers.SerializerMethodField()
    receipt_status = serializers.SerializerMethodField()
    received_item_count = serializers.SerializerMethodField()
    ordered_item_count = serializers.SerializerMethodField()
    total_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceivedNote
        fields = [
            "id", "document_number", "status", "receipt_status", "vendor_id", "vendor_code", "vendor_name", "received_by_name",
            "purchase_order_id", "purchase_order_number", "received_date", "reference", "narration",
            "received_item_count", "ordered_item_count",
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


class GoodsReceivedNoteListSerializer(GoodsReceivedNoteSerializer):
    """Lighter list row: the receipt lines belong to the detail drawer (its own
    request). ``receipt_status`` and the item counts are still computed from the
    prefetched lines — only the serialised line array is dropped from the payload."""

    lines = None

    class Meta(GoodsReceivedNoteSerializer.Meta):
        fields = [f for f in GoodsReceivedNoteSerializer.Meta.fields if f != "lines"]


# --------------------------------------------------------------------------- #
# Vendor invoice                                                              #
# --------------------------------------------------------------------------- #

class VendorInvoiceLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True)

    class Meta:
        model = VendorInvoiceLine
        fields = [
            "id", "line_no", "po_line_id", "grn_line_id", "description",
            "expense_account_id", "expense_code",
            "quantity", "unit_price", "tax_code_id", "net_amount", "tax_amount",
        ]


class VendorInvoiceSerializer(serializers.ModelSerializer):
    lines = VendorInvoiceLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    purchase_order_number = serializers.CharField(
        source="purchase_order.document_number", read_only=True, default=None,
    )
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    display_status = serializers.SerializerMethodField()

    class Meta:
        model = VendorInvoice
        fields = [
            "id", "document_number", "status", "approval_state", "match_status", "payment_status",
            "display_status", "is_overdue",
            "vendor_id", "vendor_code", "vendor_name", "purchase_order_id", "purchase_order_number",
            "invoice_date", "due_date", "vendor_reference", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "journal_id", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)

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
    """List rows omit line arrays; the detail drawer fetches those separately."""

    lines = None

    class Meta(VendorInvoiceSerializer.Meta):
        fields = [f for f in VendorInvoiceSerializer.Meta.fields if f != "lines"]


# --------------------------------------------------------------------------- #
# Vendor payment                                                              #
# --------------------------------------------------------------------------- #

class VendorPaymentAllocationSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(
        source="vendor_invoice.document_number", read_only=True,
    )

    class Meta:
        model = VendorPaymentAllocation
        fields = ["id", "vendor_invoice_id", "invoice_number", "amount"]


class VendorPaymentSerializer(serializers.ModelSerializer):
    allocations = VendorPaymentAllocationSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    payment_code = serializers.CharField(source="payment_account.code", read_only=True, default=None)
    net_naira = serializers.SerializerMethodField()

    class Meta:
        model = VendorPayment
        fields = [
            "id", "document_number", "status", "vendor_id", "vendor_code",
            "payment_date", "method",
            "gross_amount", "wht_amount", "net_amount", "net_naira",
            "allocated_amount", "payment_account_id", "payment_code",
            "reference", "narration", "journal_id", "allocations",
        ]

    def get_net_naira(self, obj) -> str:
        return format_naira(obj.net_amount)
