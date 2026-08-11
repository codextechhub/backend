"""Verified runtime consumers for every editable Procurement setting."""


def _consumer(service, consumer, impact):
    return {"service": service, "consumer": consumer, "impact": impact}


PROCUREMENT_SETTING_CONSUMERS = {
    "default_payment_terms": _consumer(
        "Vendor and contract creation",
        "vs_procurement.views.vendors; vs_procurement.views.contracts",
        "Supplies payment terms when a new vendor or contract omits them.",
    ),
    "default_delivery_address": _consumer(
        "Purchase order creation",
        "vs_procurement.views.orders",
        "Supplies the receiving address when a purchase order omits one.",
    ),
    "quantity_tolerance_bps": _consumer(
        "Vendor invoice matching",
        "vs_procurement.payables",
        "Controls the permitted billed-quantity variance from received quantity.",
    ),
    "price_tolerance_bps": _consumer(
        "Vendor invoice matching",
        "vs_procurement.payables",
        "Controls the permitted vendor-invoice price variance from the purchase order.",
    ),
    "allow_non_po_invoices": _consumer(
        "Vendor invoice matching",
        "vs_procurement.payables",
        "Allows or blocks invoice processing without purchase-order evidence.",
    ),
    "vendor_purchase_kyc_requirement": _consumer(
        "Vendor eligibility",
        "vs_procurement.purchasing",
        "Determines which vendor KYC states can be used for purchasing.",
    ),
    "require_purchase_order_for_receipts": _consumer(
        "Goods receiving",
        "vs_procurement.views.receiving",
        "Requires purchase-order evidence before a goods receipt can be recorded.",
    ),
    "default_requisition_lead_days": _consumer(
        "Purchase requisitions",
        "vs_procurement.views.requisitions",
        "Calculates needed-by when a requisition omits that date.",
    ),
    "contract_renewal_notice_days": _consumer(
        "Contract creation and renewals",
        "vs_procurement.views.contracts",
        "Copies the default renewal-notice window to new contracts.",
    ),
    "default_rfq_response_days": _consumer(
        "Request for quotation",
        "vs_procurement.views.orders",
        "Calculates the response deadline when a new RFQ omits one.",
    ),
    "rfq_closing_soon_days": _consumer(
        "RFQ monitoring",
        "vs_procurement.views.orders",
        "Controls how early an open RFQ appears as closing soon.",
    ),
    "minimum_rfq_invited_vendors": _consumer(
        "Competitive bidding governance",
        "vs_procurement.sourcing",
        "Blocks RFQ issue below the invited-vendor minimum unless an exception is authorized.",
    ),
    "minimum_submitted_quotations_before_award": _consumer(
        "Competitive bidding governance",
        "vs_procurement.sourcing",
        "Blocks award below the submitted-quotation minimum unless an exception is authorized.",
    ),
}
