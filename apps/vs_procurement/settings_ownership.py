"""What each editable Procurement setting drives, in words a settings screen can show.

Every entry names the service that reads the setting and the effect a change
has on it. Both are plain English for the person changing the value: the code
that reads a setting is never named here, because these maps are sent to the
client as they stand and a module path would describe the server's layout to
anyone who can open the screen.
"""


def _consumer(service, impact):
    return {"service": service, "impact": impact}


PROCUREMENT_SETTING_CONSUMERS = {
    "default_payment_terms": _consumer(
        "Vendor and contract creation",
        "Supplies payment terms when a new vendor or contract omits them.",
    ),
    "default_delivery_address": _consumer(
        "Purchase order creation",
        "Supplies the receiving address when a purchase order omits one.",
    ),
    "quantity_tolerance_bps": _consumer(
        "Vendor invoice matching",
        "Controls the permitted billed-quantity variance from received quantity.",
    ),
    "price_tolerance_bps": _consumer(
        "Vendor invoice matching",
        "Controls the permitted vendor-invoice price variance from the purchase order.",
    ),
    "allow_non_po_invoices": _consumer(
        "Vendor invoice matching",
        "Allows or blocks invoice processing without purchase-order evidence.",
    ),
    "vendor_purchase_kyc_requirement": _consumer(
        "Vendor eligibility",
        "Determines which vendor KYC states can be used for purchasing.",
    ),
    "require_purchase_order_for_receipts": _consumer(
        "Goods receiving",
        "Requires purchase-order evidence before a goods receipt can be recorded.",
    ),
    "default_requisition_lead_days": _consumer(
        "Purchase requisitions",
        "Calculates needed-by when a requisition omits that date.",
    ),
    "contract_renewal_notice_days": _consumer(
        "Contract creation and renewals",
        "Copies the default renewal-notice window to new contracts.",
    ),
    "default_rfq_response_days": _consumer(
        "Request for quotation",
        "Calculates the response deadline when a new RFQ omits one.",
    ),
    "rfq_closing_soon_days": _consumer(
        "RFQ monitoring",
        "Controls how early an open RFQ appears as closing soon.",
    ),
    "minimum_rfq_invited_vendors": _consumer(
        "Competitive bidding governance",
        "Blocks RFQ issue below the invited-vendor minimum unless an exception is authorized.",
    ),
    "minimum_submitted_quotations_before_award": _consumer(
        "Competitive bidding governance",
        "Blocks award below the submitted-quotation minimum unless an exception is authorized.",
    ),
    "non_po_spend_limit_pct": _consumer(
        "Procurement dashboard",
        "Marks spend on bills without a purchase order as over the school's limit.",
    ),
}
