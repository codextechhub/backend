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
    AccountMappingKey.VENDOR_ADVANCE: _consumer(
        "Vendor prepayments",
        "vs_procurement.payables",
        "Holds money paid to a vendor before their bill exists, until a bill draws it down.",
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
        "Receivable write-offs and doubtful-debt provisions",
        "vs_finance.credit_notes; vs_finance.provisions",
        "Posts the part of a write-off the allowance does not cover, and each "
        "provision run's movement in the allowance.",
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
    AccountMappingKey.DEFERRED_INCOME: _consumer(
        "Deferred income",
        "vs_finance.receivables; vs_finance.deferred_income",
        "Holds invoiced amounts whose service period has not started, until each "
        "month's share is released to revenue.",
    ),
    AccountMappingKey.DEPOSITS_HELD: _consumer(
        "Customer deposits",
        "vs_finance.receivables; vs_finance.deposits",
        "Holds refundable deposits billed to customers until they are returned, set "
        "against bills or forfeited.",
    ),
    AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE: _consumer(
        "Doubtful-debt provision",
        "vs_finance.provisions; vs_finance.credit_notes",
        "Carries the allowance each provision run sets and each write-off uses.",
    ),
    AccountMappingKey.BAD_DEBT_RECOVERED: _consumer(
        "Bad-debt recovery",
        "vs_finance.credit_notes.recover_write_off",
        "Receives the income when a written-off debt is later paid.",
    ),
    AccountMappingKey.FORFEITED_DEPOSIT_INCOME: _consumer(
        "Unclaimed deposits",
        "vs_finance.deposits.forfeit_unclaimed_deposits",
        "Receives deposits left unclaimed past the entity's limit.",
    ),
    AccountMappingKey.GATEWAY_CLEARING: _consumer(
        "Online collections",
        "vs_payments.services._book_receipt; vs_payments.settlement.settle_collections",
        "Holds confirmed online payments until the provider's settlement reaches a bank.",
    ),
    AccountMappingKey.CLIENT_FUNDS_HELD: _consumer(
        "Held online money (platform books)",
        "vs_payments.held",
        "Carries what the platform's provider balance holds for each client branch, "
        "raised by its online payments and lowered by its settlements and payouts.",
    ),
    AccountMappingKey.CLIENT_FUNDS_OWED: _consumer(
        "Held online money (platform books)",
        "vs_payments.held",
        "Carries what a client branch owes the platform when a chargeback took more "
        "than the platform held for it; its next payments and settlement repay it.",
    ),
    AccountMappingKey.CHARGEBACKS: _consumer(
        "Chargebacks on held online payments",
        "vs_payments.held.record_dispute",
        "Receives the loss when a payer's bank takes back an online payment the "
        "platform held for the branch.",
    ),
    AccountMappingKey.INTER_BRANCH: _consumer(
        "Inter-branch transfers",
        "vs_finance.inter_branch",
        "Carries what each branch is owed by or owes to each other branch: cash lent, "
        "costs recharged, goods issued and customers' balances moved.",
    ),
    AccountMappingKey.HELD_FOR_OTHER_BRANCHES: _consumer(
        "Money received for another branch",
        "vs_finance.inter_branch",
        "Holds a payment one branch received that belongs to another, until it is forwarded.",
    ),
    AccountMappingKey.PROVIDER_BALANCE: _consumer(
        "Held online money (platform books)",
        "vs_payments.held",
        "Carries the platform's payment provider balance, the asset behind the "
        "client funds it holds.",
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
        "Prints each branch's payment destination on its customer documents.",
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
    "auto_apply_customer_credit": _consumer(
        "Customer credit",
        "vs_finance.receivables.apply_customer_credit",
        "Applies a customer's unapplied credit to each new invoice of theirs as it posts.",
    ),
    "concession_second_person_threshold": _consumer(
        "Concessions",
        "vs_finance.installments",
        "Above this running total a concession must be posted by somebody other than its author.",
    ),
    "term_collection_target_pct": _consumer(
        "Finance dashboard: receivables",
        "vs_finance.dashboard_receivables",
        "Draws the target line on the collection curve and judges the projected finish against it.",
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


RECEIVABLES_SETTING_CONSUMERS = {
    "revenue_recognition": _consumer(
        "Deferred income",
        "vs_finance.deferred_income.schedule_line",
        "Decides how an invoice line billed before its service period becomes revenue: "
        "spread over each month of the period, or all in the month it starts.",
    ),
    "provision_bands": _consumer(
        "Doubtful-debt provision",
        "vs_finance.provisions.required_allowance",
        "Sets the share of each overdue balance the allowance must cover, by age.",
    ),
    "deposits_offset_unpaid_bills": _consumer(
        "Customer deposits",
        "vs_finance.deposits.on_customer_left",
        "Allows a departing customer's deposit to settle their unpaid bills before "
        "the rest is returned.",
    ),
    "unclaimed_deposit_years": _consumer(
        "Customer deposits",
        "vs_finance.deposits.forfeit_unclaimed_deposits",
        "Years after a customer leaves before an unclaimed deposit may be taken to income.",
    ),
}


CALENDAR_SETTING_CONSUMERS = {
    "next_year_mode": _consumer(
        "Fiscal calendar rollover",
        "vs_finance.tasks.roll_fiscal_calendars",
        "Decides whether the next fiscal year is opened automatically or finance staff are only warned.",
    ),
    "next_year_lead_days": _consumer(
        "Fiscal calendar rollover and runway warning",
        "vs_finance.fiscal_calendar; vs_finance.posting.fiscal_calendar_runway",
        "Sets how many days before the calendar ends the next year is opened or the warning starts.",
    ),
}

PAYROLL_SETTING_CONSUMERS = {
    "paye_method": _consumer(
        "PAYE and employee pension on generated runs",
        "vs_finance.payroll_statutory.work_out_line",
        "Computes PAYE from the national tax table and pension from the rate, or takes both "
        "from the salary structure or roster figures.",
    ),
    "tax_country": _consumer(
        "PAYE tax table",
        "vs_finance.payroll_tax.table_for",
        "Picks which country's national tax tables price PAYE.",
    ),
    "employee_pension_enabled": _consumer(
        "Employee pension", "vs_finance.payroll_statutory.work_out_line",
        "Withholds the employee's pension contribution from pensionable pay.",
    ),
    "employee_pension_rate_bps": _consumer(
        "Employee pension", "vs_finance.payroll_statutory.work_out_line",
        "Rate of pensionable pay withheld as the employee's pension.",
    ),
    "employer_pension_enabled": _consumer(
        "Employer pension", "vs_finance.payroll_statutory.work_out_line",
        "Accrues the employer's pension contribution on top of pay, per branch.",
    ),
    "employer_pension_rate_bps": _consumer(
        "Employer pension", "vs_finance.payroll_statutory.work_out_line",
        "Rate of pensionable pay the employer contributes.",
    ),
    "nhf_enabled": _consumer(
        "National Housing Fund", "vs_finance.payroll_statutory.work_out_line",
        "Withholds NHF from the employee's basic pay.",
    ),
    "nhf_rate_bps": _consumer(
        "National Housing Fund", "vs_finance.payroll_statutory.work_out_line",
        "Rate of basic pay withheld for NHF.",
    ),
    "nsitf_enabled": _consumer(
        "NSITF employee compensation", "vs_finance.payroll_statutory.work_out_line",
        "Accrues the employer's NSITF contribution on gross pay, per branch.",
    ),
    "nsitf_rate_bps": _consumer(
        "NSITF employee compensation", "vs_finance.payroll_statutory.work_out_line",
        "Rate of gross pay the employer contributes to NSITF.",
    ),
    "itf_enabled": _consumer(
        "ITF training levy", "vs_finance.payroll_statutory.work_out_line",
        "Accrues the employer's ITF levy on gross pay, per branch.",
    ),
    "itf_rate_bps": _consumer(
        "ITF training levy", "vs_finance.payroll_statutory.work_out_line",
        "Rate of gross pay the employer accrues for the ITF levy.",
    ),
    "payslip_in_app": _consumer(
        "Payslip delivery", "vs_finance.payslips.deliver_payslips",
        "Shows each employee their own payslips in the app and sends an in-app notice.",
    ),
    "payslip_email": _consumer(
        "Payslip delivery", "vs_finance.payslips.deliver_payslips",
        "Emails each employee their payslip as a PDF when the run is paid.",
    ),
}
