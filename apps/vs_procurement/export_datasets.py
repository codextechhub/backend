"""Procurement datasets published to the Export Centre.

Registered from :meth:`vs_procurement.apps.VsProcurementConfig.ready`. Entity-scoped
like finance: a purchase order belongs to the set of books that will pay it.

The vendor master carries the platform's most sensitive master data - bank account
details and tax IDs - so those columns need ``exports.sensitive_field.export`` on top
of the ordinary vendor read.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_BOOLEAN,
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATE,
    KIND_DATETIME,
    KIND_MONEY,
    KIND_TEXT,
    Dataset,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the entity-scoped base queryset for purchase orders.
def _purchase_orders(scope):
    from .models import PurchaseOrder

    return PurchaseOrder.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for vendor invoices.
def _vendor_invoices(scope):
    from .models import VendorInvoice

    return VendorInvoice.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for the vendor master.
def _vendors(scope):
    from .models import Vendor

    return Vendor.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for purchase requisitions.
def _requisitions(scope):
    from .models import PurchaseRequisition

    return PurchaseRequisition.objects.filter(entity=scope.entity)


_DOC_STATUS = choice_labels("vs_finance.constants.DocumentStatus")
_KYC_STATUS = choice_labels("vs_procurement.constants.VendorKycStatus")
_MATCH_STATUS = choice_labels("vs_procurement.constants.MatchStatus")


# Register every procurement dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="procurement.purchase_orders",
        module="Procurement",
        name="Purchase orders",
        description=(
            "One row per purchase order, with vendor, dates and approval state. What "
            "a buyer needs to answer 'what have we committed to spend'."
        ),
        base=_purchase_orders,
        permission="procurement.purchase_order.view",
        row_cap=200_000,
        default_columns=("document_number", "vendor_name", "order_date", "status"),
        fields=(
            Field("document_number", "PO number", "Order", KIND_TEXT, locked=True),
            Field("vendor_name", "Vendor", "Order", KIND_TEXT, source="vendor__name"),
            Field("vendor_code", "Vendor code", "Order", KIND_TEXT, source="vendor__code"),
            Field("order_date", "Order date", "Order", KIND_DATE),
            Field("expected_date", "Expected", "Order", KIND_DATE),
            Field("status", "Status", "Order", KIND_CHOICE, choices=_DOC_STATUS),
            Field("approval_state", "Approval", "Order", KIND_TEXT),
            Field("currency", "Currency", "Order", KIND_TEXT, source="currency__code"),
            Field("payment_terms", "Payment terms", "Order", KIND_TEXT),
            Field("requisition_number", "From requisition", "Order", KIND_TEXT,
                  source="requisition__document_number"),
            Field("contract_reference", "Contract", "Order", KIND_TEXT,
                  source="contract__reference"),
            Field("reference", "Reference", "Order", KIND_TEXT),
            Field("narration", "Narration", "Order", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "PO number"), ("vendor__name", "Vendor"),
                ("reference", "Reference"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("order_date", "Order date", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("vendor", "Vendor", FILTER_TEXT, source="vendor__name"),
        ),
    ))

    register(Dataset(
        key="procurement.vendor_invoices",
        module="Procurement",
        name="Vendor invoices",
        description=(
            "Supplier bills with their three-way match and payment state. The dataset "
            "behind an accounts-payable ageing."
        ),
        base=_vendor_invoices,
        permission="procurement.vendor_invoice.view",
        row_cap=200_000,
        default_columns=("document_number", "vendor_name", "invoice_date", "due_date", "status"),
        fields=(
            Field("document_number", "Invoice number", "Invoice", KIND_TEXT, locked=True),
            Field("vendor_name", "Vendor", "Invoice", KIND_TEXT, source="vendor__name"),
            Field("vendor_reference", "Vendor's reference", "Invoice", KIND_TEXT),
            Field("invoice_date", "Invoice date", "Invoice", KIND_DATE),
            Field("due_date", "Due date", "Invoice", KIND_DATE),
            Field("status", "Status", "Invoice", KIND_CHOICE, choices=_DOC_STATUS),
            Field("payment_status", "Payment status", "Invoice", KIND_TEXT),
            Field("match_status", "Match status", "Invoice", KIND_CHOICE, choices=_MATCH_STATUS),
            Field("approval_state", "Approval", "Invoice", KIND_TEXT),
            Field("po_number", "Purchase order", "Invoice", KIND_TEXT,
                  source="purchase_order__document_number"),
            Field("currency", "Currency", "Invoice", KIND_TEXT, source="currency__code"),
            Field("narration", "Narration", "Invoice", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "Invoice number"), ("vendor__name", "Vendor"),
                ("vendor_reference", "Vendor's reference"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("invoice_date", "Invoice date", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("match_status", "Match status", FILTER_CHOICE, choices=_MATCH_STATUS),
            FilterDef("vendor", "Vendor", FILTER_TEXT, source="vendor__name"),
        ),
    ))

    register(Dataset(
        key="procurement.vendors",
        module="Procurement",
        name="Vendor master",
        description=(
            "The supplier list with KYC state, risk rating and payment terms. Master "
            "data, so no date filter is required. Banking and tax columns are restricted."
        ),
        base=_vendors,
        permission="procurement.vendor.view",
        row_cap=100_000,
        default_columns=("code", "name", "kyc_status", "is_active"),
        fields=(
            Field("code", "Vendor code", "Vendor", KIND_TEXT, locked=True),
            Field("name", "Name", "Vendor", KIND_TEXT),
            Field("category", "Category", "Vendor", KIND_TEXT, source="category__name"),
            Field("kyc_status", "KYC status", "Vendor", KIND_CHOICE, choices=_KYC_STATUS),
            Field("risk", "Risk", "Vendor", KIND_TEXT),
            Field("on_hold", "On hold", "Vendor", KIND_TEXT),
            Field("is_active", "Active", "Vendor", KIND_TEXT),
            Field("payment_terms", "Payment terms", "Vendor", KIND_TEXT),
            Field("payable_account", "Payable account", "Vendor", KIND_TEXT,
                  source="payable_account__code"),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("email", "Email", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: supplier contact data."),
            Field("phone", "Phone", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: supplier contact data."),
            Field("address", "Address", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: supplier contact data."),
            Field("tax_id", "Tax ID", "Banking", KIND_TEXT, sensitive=True,
                  description="Restricted: supplier tax registration."),
            Field("bank_name", "Bank", "Banking", KIND_TEXT, sensitive=True,
                  description="Restricted: supplier banking data."),
            Field("bank_account_number", "Bank account number", "Banking", KIND_TEXT,
                  sensitive=True, description="Restricted: supplier banking data."),
            Field("bank_account_name", "Bank account name", "Banking", KIND_TEXT,
                  sensitive=True, description="Restricted: supplier banking data."),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("kyc_status", "KYC status", FILTER_CHOICE, choices=_KYC_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("code", "Vendor code"), ("name", "Name"),
            ), description="Matches either one, the way the search box does."),
            FilterDef("is_active", "Active", FILTER_BOOLEAN),
            FilterDef("on_hold", "On hold", FILTER_BOOLEAN),
            FilterDef("name", "Name", FILTER_TEXT),
        ),
    ))

    register(Dataset(
        key="procurement.requisitions",
        module="Procurement",
        name="Purchase requisitions",
        description=(
            "What people asked to buy, and how far each request got. Useful for "
            "spotting demand that never turned into an order."
        ),
        base=_requisitions,
        permission="procurement.requisition.view",
        row_cap=200_000,
        default_columns=("document_number", "title", "request_date", "status"),
        fields=(
            Field("document_number", "Requisition number", "Requisition", KIND_TEXT, locked=True),
            Field("title", "Title", "Requisition", KIND_TEXT),
            Field("request_date", "Requested on", "Requisition", KIND_DATE),
            Field("needed_by", "Needed by", "Requisition", KIND_DATE),
            Field("status", "Status", "Requisition", KIND_CHOICE, choices=_DOC_STATUS),
            Field("approval_state", "Approval", "Requisition", KIND_TEXT),
            Field("estimated_total", "Estimated total", "Amounts", KIND_MONEY),
            Field("cost_center", "Cost centre", "Analysis", KIND_TEXT, source="cost_center__code"),
            Field("justification", "Justification", "Requisition", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("request_date", "Requested on", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "Requisition number"), ("title", "Title"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("cost_center", "Cost centre", FILTER_TEXT,
                      source="cost_center__code"),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the purchase-order list screen's filters into export filters.
def _translate_purchase_orders(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("vendor"):
        filters.append({"id": "vendor", "value": value})
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    if value := params.get("rfq"):
        unmapped.append(Unmapped(
            "rfq", value,
            "The purchase-order export cannot filter by the RFQ an order came from.",
        ))
    return filters, unmapped


# Translate the vendor list screen's filters into export filters.
def _translate_vendors(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    truthy = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}

    if value := params.get("kyc_status"):
        filters.append({"id": "kyc_status", "values": [value]})
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    for key in ("is_active", "on_hold"):
        raw = params.get(key)
        if raw is None:
            continue
        parsed = truthy.get(str(raw).lower())
        if parsed is None:
            unmapped.append(Unmapped(key, raw, "Not a yes/no value the export understands."))
        else:
            filters.append({"id": key, "value": parsed})
    if value := params.get("purchase_eligible"):
        unmapped.append(Unmapped(
            "purchase_eligible", value,
            "Purchase eligibility is worked out from KYC, hold state and contract "
            "cover together, so it has no single export filter. Filter on KYC status "
            "and hold instead.",
        ))
    return filters, unmapped


# Translate the vendor-invoice list screen's filters into export filters.
def _translate_vendor_invoices(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("match_status"):
        filters.append({"id": "match_status", "values": [value]})
    if value := params.get("vendor"):
        filters.append({"id": "vendor", "value": value})
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    return filters, unmapped


# Translate the requisition list screen's filters into export filters.
def _translate_requisitions(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    if value := params.get("cost_center"):
        filters.append({"id": "cost_center", "value": value})
    return filters, unmapped


# Register the procurement screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="procurement.purchase_orders",
        handles=(
            "status", "vendor", "q", "search", "rfq",
        ),
        label="Procurement - Purchase orders",
        dataset_key="procurement.purchase_orders",
        translate=_translate_purchase_orders,
    ))
    register_screen(ScreenBinding(
        key="procurement.vendors",
        handles=(
            "kyc_status", "is_active", "on_hold", "purchase_eligible", "q", "search",
        ),
        label="Procurement - Vendors",
        dataset_key="procurement.vendors",
        translate=_translate_vendors,
    ))
    register_screen(ScreenBinding(
        key="procurement.vendor_invoices",
        handles=(
            "status", "match_status", "vendor", "q", "search",
        ),
        label="Procurement - Vendor invoices",
        dataset_key="procurement.vendor_invoices",
        translate=_translate_vendor_invoices,
    ))
    register_screen(ScreenBinding(
        key="procurement.requisitions",
        handles=(
            "status", "q", "search", "cost_center",
        ),
        label="Procurement - Requisitions",
        dataset_key="procurement.requisitions",
        translate=_translate_requisitions,
    ))
