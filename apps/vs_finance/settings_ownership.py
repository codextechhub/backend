"""Verified runtime consumers for every editable Finance setting."""

from .constants import AccountMappingKey


def _consumer(service, consumer, impact):
    return {"service": service, "consumer": consumer, "impact": impact}


ACCOUNT_MAPPING_CONSUMERS = {
    AccountMappingKey.CASH_BANK: _consumer(
        "Cash reporting and journals",
        "vs_finance.account_mappings.resolve_mapped_account",
        "Selects the primary cash account for operational journals and cash reports.",
    ),
    AccountMappingKey.ACCOUNTS_RECEIVABLE: _consumer(
        "Accounts receivable posting",
        "vs_finance.views_ar",
        "Posts and validates customer control-account balances.",
    ),
    AccountMappingKey.ACCOUNTS_PAYABLE: _consumer(
        "Procurement payables posting",
        "vs_procurement.views.vendors",
        "Posts vendor liabilities into the Finance control account.",
    ),
    AccountMappingKey.CUSTOMER_CREDIT: _consumer(
        "Collections and refunds",
        "vs_finance.receivables",
        "Holds unapplied receipts, overpayments, credit notes and refundable credit.",
    ),
    AccountMappingKey.GRIR_CLEARING: _consumer(
        "Goods received and invoice received",
        "vs_procurement.reports",
        "Reconciles received goods against matched vendor invoices.",
    ),
    AccountMappingKey.OUTPUT_VAT: _consumer(
        "Sales tax posting",
        "vs_finance.account_mappings.resolve_default_code_mapping",
        "Routes sales-tax liabilities when a document uses the standard output VAT role.",
    ),
    AccountMappingKey.WHT_PAYABLE: _consumer(
        "Supplier withholding posting",
        "vs_finance.account_mappings.resolve_default_code_mapping",
        "Routes supplier withholding liabilities into the configured payable account.",
    ),
    AccountMappingKey.RETAINED_EARNINGS: _consumer(
        "Year-end close",
        "vs_finance.close",
        "Receives year-end profit or loss and approved opening-balance equity.",
    ),
    AccountMappingKey.BAD_DEBT_EXPENSE: _consumer(
        "Receivable write-offs",
        "vs_finance.credit_notes",
        "Posts approved customer-balance write-offs to expense.",
    ),
    AccountMappingKey.BANK_CHARGES: _consumer(
        "Bank reconciliation adjustments",
        "vs_finance.banking",
        "Supplies the expense side of an authorized bank-charge adjustment.",
    ),
    AccountMappingKey.INVENTORY_ASSET: _consumer(
        "Inventory posting",
        "vs_finance.account_mappings.resolve_default_code_mapping",
        "Routes stock receipts and issues to the inventory asset role.",
    ),
    AccountMappingKey.INVENTORY_ADJUSTMENT: _consumer(
        "Inventory adjustment posting",
        "vs_finance.account_mappings.resolve_default_code_mapping",
        "Routes stock-count gains, losses and write-downs.",
    ),
    AccountMappingKey.PURCHASE_PRICE_VARIANCE: _consumer(
        "Vendor invoice matching",
        "vs_finance.account_mappings.resolve_default_code_mapping",
        "Routes permitted receipt-to-invoice price differences.",
    ),
}


DOCUMENT_SETTING_CONSUMERS = {
    "default_invoice_due_days": _consumer(
        "Customer invoicing",
        "vs_finance.views; vs_finance.views_ar",
        "Calculates the due date when manual or fee-generated invoices omit one.",
    ),
    "primary_collection_bank_account": _consumer(
        "Invoice and receipt presentation",
        "vs_finance.documents._issuer_block",
        "Prints the preferred payment destination on customer documents.",
    ),
    "default_invoice_narration": _consumer(
        "Manual invoicing",
        "vs_finance.views",
        "Supplies narration when a new manual invoice does not provide one.",
    ),
    "auto_post_manual_invoices": _consumer(
        "Manual invoicing",
        "vs_finance.views",
        "Decides whether a new manual invoice posts immediately or remains a draft.",
    ),
    "allow_customer_opening_balances": _consumer(
        "Customer master data",
        "vs_finance.views_ar",
        "Allows or rejects non-zero customer opening balances.",
    ),
}


BANKING_SETTING_CONSUMERS = {
    "default_bank_reconciliation_tolerance_days": _consumer(
        "Bank reconciliation",
        "vs_finance.views_ops.banking",
        "Sets the default date window for automatic statement matching.",
    ),
    "default_group_reconciliation_matches": _consumer(
        "Bank reconciliation",
        "vs_finance.views_ops.banking",
        "Allows one statement line to match a uniquely determined ledger-line group.",
    ),
    "default_receipt_allocation_strategy": _consumer(
        "Receipt allocation",
        "vs_finance.views_ar",
        "Orders invoices when a receipt is allocated without an explicit user choice.",
    ),
    "petty_cash_low_balance_threshold_bps": _consumer(
        "Petty cash monitoring",
        "vs_finance.views_ops.pettycash",
        "Determines when a live petty-cash balance is flagged for replenishment.",
    ),
}
