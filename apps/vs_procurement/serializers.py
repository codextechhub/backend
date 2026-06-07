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

from vs_finance.money import format_naira

from .models import (
    GoodsReceivedNote,
    GoodsReceivedNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    Vendor,
    VendorCategory,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
    VendorPaymentAllocation,
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


class VendorSerializer(serializers.ModelSerializer):
    category_code = serializers.CharField(source="category.code", read_only=True, default=None)
    payable_code = serializers.CharField(source="payable_account.code", read_only=True, default=None)
    default_expense_code = serializers.CharField(
        source="default_expense_account.code", read_only=True, default=None,
    )

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
# Purchase requisition                                                        #
# --------------------------------------------------------------------------- #

class RequisitionLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)
    estimated_line_total = serializers.IntegerField(read_only=True)

    class Meta:
        model = PurchaseRequisitionLine
        fields = [
            "id", "line_no", "description", "quantity", "estimated_unit_price",
            "expense_account_id", "expense_code", "tax_code_id", "estimated_line_total",
        ]


class RequisitionSerializer(serializers.ModelSerializer):
    lines = RequisitionLineSerializer(many=True, read_only=True)
    estimated_total_naira = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseRequisition
        fields = [
            "id", "document_number", "status", "request_date", "needed_by",
            "justification", "estimated_total", "estimated_total_naira", "lines",
        ]

    def get_estimated_total_naira(self, obj) -> str:
        return format_naira(obj.estimated_total)


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


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = POLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    total_naira = serializers.SerializerMethodField()
    received_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    invoiced_pct = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "document_number", "status", "vendor_id", "vendor_code",
            "requisition_id", "order_date", "expected_date", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "received_pct", "invoiced_pct", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


# --------------------------------------------------------------------------- #
# Goods received note                                                         #
# --------------------------------------------------------------------------- #

class GRNLineSerializer(serializers.ModelSerializer):
    expense_code = serializers.CharField(source="expense_account.code", read_only=True)

    class Meta:
        model = GoodsReceivedNoteLine
        fields = [
            "id", "line_no", "po_line_id", "description",
            "expense_account_id", "expense_code",
            "accepted_qty", "rejected_qty", "unit_price", "value_amount",
        ]


class GoodsReceivedNoteSerializer(serializers.ModelSerializer):
    lines = GRNLineSerializer(many=True, read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    total_value_naira = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReceivedNote
        fields = [
            "id", "document_number", "status", "vendor_id", "vendor_code",
            "purchase_order_id", "received_date", "reference", "narration",
            "total_value", "total_value_naira", "journal_id", "lines",
        ]

    def get_total_value_naira(self, obj) -> str:
        return format_naira(obj.total_value)


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
    balance_due = serializers.IntegerField(read_only=True)
    total_naira = serializers.SerializerMethodField()

    class Meta:
        model = VendorInvoice
        fields = [
            "id", "document_number", "status", "match_status", "payment_status",
            "vendor_id", "vendor_code", "purchase_order_id",
            "invoice_date", "due_date", "vendor_reference", "narration",
            "subtotal", "tax_total", "total", "total_naira",
            "amount_paid", "balance_due", "journal_id", "lines",
        ]

    def get_total_naira(self, obj) -> str:
        return format_naira(obj.total)


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
