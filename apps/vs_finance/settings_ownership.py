"""What each editable Finance setting drives, in words a settings screen can show.

Every entry names the service that reads the setting and the effect a change
has on it. Both are plain English for the person changing the value: the code
that reads a setting is never named here, because these maps are sent to the
client as they stand and a module path would describe the server's layout to
anyone who can open the screen.
"""

from .constants import AccountMappingKey


def _consumer(service, impact):
    return {"service": service, "impact": impact}


ACCOUNT_MAPPING_CONSUMERS = {
    AccountMappingKey.CASH_BANK: _consumer(
        "Cash reporting and journals",
        "Selects the primary cash account for operational journals and cash reports.",
    ),
    AccountMappingKey.ACCOUNTS_RECEIVABLE: _consumer(
        "Accounts receivable posting",
        "Posts and validates customer control-account balances.",
    ),
    AccountMappingKey.ACCOUNTS_PAYABLE: _consumer(
        "Procurement payables posting",
        "Posts vendor liabilities into the Finance control account.",
    ),
    AccountMappingKey.CUSTOMER_CREDIT: _consumer(
        "Collections and refunds",
        "Holds unapplied receipts, overpayments, credit notes and refundable credit.",
    ),
    AccountMappingKey.VENDOR_ADVANCE: _consumer(
        "Vendor prepayments",
        "Holds money paid to a vendor before their bill exists, until a bill draws it down.",
    ),
    AccountMappingKey.GRIR_CLEARING: _consumer(
        "Goods received and invoice received",
        "Reconciles received goods against matched vendor invoices.",
    ),
    AccountMappingKey.OUTPUT_VAT: _consumer(
        "Sales tax posting",
        "Routes sales-tax liabilities when a document uses the standard output VAT role.",
    ),
    AccountMappingKey.WHT_PAYABLE: _consumer(
        "Supplier withholding posting",
        "Routes supplier withholding liabilities into the configured payable account.",
    ),
    AccountMappingKey.RETAINED_EARNINGS: _consumer(
        "Year-end close",
        "Receives year-end profit or loss and approved opening-balance equity.",
    ),
    AccountMappingKey.BAD_DEBT_EXPENSE: _consumer(
        "Receivable write-offs and doubtful-debt provisions",
        "Posts the part of a write-off the allowance does not cover, and each "
        "provision run's movement in the allowance.",
    ),
    AccountMappingKey.BANK_CHARGES: _consumer(
        "Bank reconciliation adjustments",
        "Supplies the expense side of an authorized bank-charge adjustment.",
    ),
    AccountMappingKey.INVENTORY_ASSET: _consumer(
        "Inventory posting",
        "Routes stock receipts and issues to the inventory asset role.",
    ),
    AccountMappingKey.INVENTORY_ADJUSTMENT: _consumer(
        "Inventory adjustment posting",
        "Routes stock-count gains, losses and write-downs.",
    ),
    AccountMappingKey.PURCHASE_PRICE_VARIANCE: _consumer(
        "Vendor invoice matching",
        "Routes permitted receipt-to-invoice price differences.",
    ),
    AccountMappingKey.DEFERRED_INCOME: _consumer(
        "Deferred income",
        "Holds invoiced amounts whose service period has not started, until each "
        "month's share is released to revenue.",
    ),
    AccountMappingKey.DEPOSITS_HELD: _consumer(
        "Customer deposits",
        "Holds refundable deposits billed to customers until they are returned, set "
        "against bills or forfeited.",
    ),
    AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE: _consumer(
        "Doubtful-debt provision",
        "Carries the allowance each provision run sets and each write-off uses.",
    ),
    AccountMappingKey.BAD_DEBT_RECOVERED: _consumer(
        "Bad-debt recovery",
        "Receives the income when a written-off debt is later paid.",
    ),
    AccountMappingKey.FORFEITED_DEPOSIT_INCOME: _consumer(
        "Unclaimed deposits",
        "Receives deposits left unclaimed past the entity's limit.",
    ),
    AccountMappingKey.GATEWAY_CLEARING: _consumer(
        "Online collections",
        "Holds confirmed online payments until the provider's settlement reaches a bank.",
    ),
    AccountMappingKey.CLIENT_FUNDS_HELD: _consumer(
        "Held online money (platform books)",
        "Carries what the platform's provider balance holds for each client branch, "
        "raised by its online payments and lowered by its settlements and payouts.",
    ),
    AccountMappingKey.CLIENT_FUNDS_OWED: _consumer(
        "Held online money (platform books)",
        "Carries what a client branch owes the platform when a chargeback took more "
        "than the platform held for it; its next payments and settlement repay it.",
    ),
    AccountMappingKey.CHARGEBACKS: _consumer(
        "Chargebacks on held online payments",
        "Receives the loss when a payer's bank takes back an online payment the "
        "platform held for the branch.",
    ),
    AccountMappingKey.INTER_BRANCH: _consumer(
        "Inter-branch transfers",
        "Carries what each branch is owed by or owes to each other branch: cash lent, "
        "costs recharged, goods issued and customers' balances moved.",
    ),
    AccountMappingKey.HELD_FOR_OTHER_BRANCHES: _consumer(
        "Money received for another branch",
        "Holds a payment one branch received that belongs to another, until it is forwarded.",
    ),
    AccountMappingKey.CASH_OVER_SHORT: _consumer(
        "Petty cash counts",
        "Receives what a petty cash count finds short, or over, against the fund's books "
        "when cash goes back to the bank.",
    ),
    AccountMappingKey.PROVIDER_BALANCE: _consumer(
        "Held online money (platform books)",
        "Carries the platform's payment provider balance, the asset behind the "
        "client funds it holds.",
    ),
}


DOCUMENT_SETTING_CONSUMERS = {
    "default_invoice_due_days": _consumer(
        "Customer invoicing",
        "Calculates the due date when manual or fee-generated invoices omit one.",
    ),
    "primary_collection_bank_account": _consumer(
        "Invoice and receipt presentation",
        "Prints each branch's payment destination on its customer documents.",
    ),
    "default_invoice_narration": _consumer(
        "Manual invoicing",
        "Supplies narration when a new manual invoice does not provide one.",
    ),
    "auto_post_manual_invoices": _consumer(
        "Manual invoicing",
        "Decides whether a new manual invoice posts immediately or remains a draft.",
    ),
    "allow_customer_opening_balances": _consumer(
        "Customer master data",
        "Allows or rejects non-zero customer opening balances.",
    ),
    "auto_apply_customer_credit": _consumer(
        "Customer credit",
        "Applies a customer's unapplied credit to each new invoice of theirs as it posts.",
    ),
    "concession_second_person_threshold": _consumer(
        "Concessions",
        "Above this running total a concession must be posted by somebody other than its author.",
    ),
    "term_collection_target_pct": _consumer(
        "Finance dashboard: receivables",
        "Draws the target line on the collection curve and judges the projected finish against it.",
    ),
}


BANKING_SETTING_CONSUMERS = {
    "default_bank_reconciliation_tolerance_days": _consumer(
        "Bank reconciliation",
        "Sets the default date window for automatic statement matching.",
    ),
    "default_group_reconciliation_matches": _consumer(
        "Bank reconciliation",
        "Allows one statement line to match a uniquely determined ledger-line group.",
    ),
    "default_receipt_allocation_strategy": _consumer(
        "Receipt allocation",
        "Orders invoices when a receipt is allocated without an explicit user choice.",
    ),
    "petty_cash_low_balance_threshold_bps": _consumer(
        "Petty cash monitoring",
        "Determines when a live petty-cash balance is flagged for replenishment.",
    ),
}


RECEIVABLES_SETTING_CONSUMERS = {
    "revenue_recognition": _consumer(
        "Deferred income",
        "Decides how an invoice line billed before its service period becomes revenue: "
        "spread over each month of the period, or all in the month it starts.",
    ),
    "provision_bands": _consumer(
        "Doubtful-debt provision",
        "Sets the share of each overdue balance the allowance must cover, by age.",
    ),
    "deposits_offset_unpaid_bills": _consumer(
        "Customer deposits",
        "Allows a departing customer's deposit to settle their unpaid bills before "
        "the rest is returned.",
    ),
    "unclaimed_deposit_years": _consumer(
        "Customer deposits",
        "Years after a customer leaves before an unclaimed deposit may be taken to income.",
    ),
    "payer_payment_split": _consumer(
        "Payments from a payer",
        "Proposes how one payment from a payer is shared among the customers it pays for.",
    ),
    "payer_payment_surplus": _consumer(
        "Payments from a payer",
        "Decides whose credit the part of a payer's payment that no bill takes becomes.",
    ),
}


CALENDAR_SETTING_CONSUMERS = {
    "next_year_mode": _consumer(
        "Fiscal calendar rollover",
        "Decides whether the next fiscal year is opened automatically or finance staff are only warned.",
    ),
    "next_year_lead_days": _consumer(
        "Fiscal calendar rollover and runway warning",
        "Sets how many days before the calendar ends the next year is opened or the warning starts.",
    ),
    "periods_close_in_order": _consumer(
        "Period close and reopen",
        "When on, a month closes only once every earlier month is closed, and reopens only "
        "while every later month is open. When off, months close and reopen in any order.",
    ),
}

PAYROLL_SETTING_CONSUMERS = {
    "paye_method": _consumer(
        "PAYE and employee pension on generated runs",
        "Computes PAYE from the national tax table and pension from the rate, or takes both "
        "from the salary structure or roster figures.",
    ),
    "tax_country": _consumer(
        "PAYE tax table",
        "Picks which country's national tax tables price PAYE.",
    ),
    "employee_pension_enabled": _consumer(
        "Employee pension",
        "Withholds the employee's pension contribution from pensionable pay.",
    ),
    "employee_pension_rate_bps": _consumer(
        "Employee pension",
        "Rate of pensionable pay withheld as the employee's pension.",
    ),
    "employer_pension_enabled": _consumer(
        "Employer pension",
        "Accrues the employer's pension contribution on top of pay, per branch.",
    ),
    "employer_pension_rate_bps": _consumer(
        "Employer pension",
        "Rate of pensionable pay the employer contributes.",
    ),
    "nhf_enabled": _consumer(
        "National Housing Fund",
        "Withholds NHF from the employee's basic pay.",
    ),
    "nhf_rate_bps": _consumer(
        "National Housing Fund",
        "Rate of basic pay withheld for NHF.",
    ),
    "nsitf_enabled": _consumer(
        "NSITF employee compensation",
        "Accrues the employer's NSITF contribution on gross pay, per branch.",
    ),
    "nsitf_rate_bps": _consumer(
        "NSITF employee compensation",
        "Rate of gross pay the employer contributes to NSITF.",
    ),
    "itf_enabled": _consumer(
        "ITF training levy",
        "Accrues the employer's ITF levy on gross pay, per branch.",
    ),
    "itf_rate_bps": _consumer(
        "ITF training levy",
        "Rate of gross pay the employer accrues for the ITF levy.",
    ),
    "payslip_in_app": _consumer(
        "Payslip delivery",
        "Shows each employee their own payslips in the app and sends an in-app notice.",
    ),
    "payslip_email": _consumer(
        "Payslip delivery",
        "Emails each employee their payslip as a PDF when the run is paid.",
    ),
    "previous_pay_required": _consumer(
        "Earlier pay from a previous employer",
        "Refuses a run that would pay somebody who joined after January before their earlier "
        "pay that tax year is recorded. Off, they are listed as a warning instead.",
    ),
    "payroll_moved_here_on": _consumer(
        "Payroll moved here mid-year",
        "The first payroll month run here by a tenant that ran payroll elsewhere earlier in "
        "the year. Staff first paid in that month are its own, not mid-year joiners.",
    ),
}
