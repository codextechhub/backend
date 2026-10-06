"""Shared enumerations and constants for the finance engine.

Defined once here so Phase-1 models (Account, FiscalPeriod, JournalEntry …) and the
posting service agree on the same vocabulary without circular imports.
"""
from __future__ import annotations

from django.db import models


# Define Period Status values.
class PeriodStatus(models.TextChoices):
    """Lifecycle of a fiscal period.

    OPEN          -> postings allowed.
    SOFT_CLOSED   -> normal users blocked; admins/auto-postings (e.g. depreciation
                     at close) may still post. Reversible.
    CLOSED        -> no postings; reversible only by an explicit re-open with audit.
    LOCKED        -> permanently sealed (e.g. after statutory filing). No postings.
    """
    OPEN = "OPEN", "Open"
    SOFT_CLOSED = "SOFT_CLOSED", "Soft Closed"
    CLOSED = "CLOSED", "Closed"
    LOCKED = "LOCKED", "Locked"


#: Statuses into which an ordinary journal may NOT be posted.
PERIOD_POSTING_BLOCKED = frozenset({
    PeriodStatus.CLOSED,
    PeriodStatus.LOCKED,
})

#: Statuses into which only privileged/system postings (close auto-entries) may go.
PERIOD_POSTING_RESTRICTED = frozenset({
    PeriodStatus.SOFT_CLOSED,
})


# Define Document Status values.
class DocumentStatus(models.TextChoices):
    """Generic lifecycle for numbered finance documents.

    Concrete documents (invoices, POs, journals) may use a subset or extend this;
    the abstract :class:`~vs_finance.models.FinanceDocument` defaults to it.
    """
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    APPROVED = "APPROVED", "Approved"
    POSTED = "POSTED", "Posted"
    REVERSED = "REVERSED", "Reversed"
    CANCELLED = "CANCELLED", "Cancelled"


#: What "pending" means wherever a finance screen counts documents that way:
#: waiting on an approver, which is what the screens' "Awaiting approval" tile
#: says. A draft is still being prepared and is not counted; a posted, reversed
#: (voided) or cancelled document is finished. The adjustments list and the
#: dashboards count from this one definition and cannot disagree.
PENDING_STATUSES = (DocumentStatus.PENDING_APPROVAL,)


# Define Doc Type values.
class DocType(models.TextChoices):
    """Document-type tokens used by the numbering sequence.

    The token becomes the prefix of a document number, e.g. ``IV`` in
    ``IV-12607221``. Keep tokens short (2 chars), uppercase, unique and
    stable - they are persisted inside human-facing identifiers.
    """
    JOURNAL = "JN", "Journal Entry"
    INVOICE = "IV", "Sales / AR Invoice"
    RECEIPT = "RC", "Receipt"
    PAYMENT = "PY", "Payment"
    CREDIT_NOTE = "CN", "Credit Note"
    DEBIT_NOTE = "DN", "Debit Note"
    REFUND = "RF", "Customer Refund"
    PAYMENT_PLAN = "PP", "Installment Payment Plan"
    CONCESSION = "CC", "Concession / Discount / Waiver"
    WRITE_OFF = "WO", "Bad-debt Write-off"
    DUNNING_NOTICE = "DU", "Dunning / Payment Reminder"
    PURCHASE_REQUISITION = "PR", "Purchase Requisition"
    RFQ = "RQ", "Request for Quotation"
    QUOTATION = "QT", "Vendor Quotation"
    PURCHASE_ORDER = "PO", "Purchase Order"
    GOODS_RECEIVED = "GN", "Goods Received Note"
    VENDOR_INVOICE = "VI", "Vendor Invoice"
    VENDOR_PAYMENT = "VP", "Vendor Payment"
    EXPENSE_CLAIM = "EX", "Expense Claim"
    PETTY_CASH_VOUCHER = "PC", "Petty Cash Voucher"
    PAYROLL_RUN = "PL", "Payroll Run"
    FIXED_ASSET = "FA", "Fixed Asset"
    TAX_FILING = "TX", "Tax Filing / Remittance"
    BUDGET = "BG", "Budget"
    BANK_TRANSACTION = "BT", "Bank Transaction"
    BANK_TRANSFER = "BX", "Transfer Between Own Accounts"
    CUSTOMER_CREDIT_TRANSFER = "CT", "Customer Credit Transfer"
    DOUBTFUL_DEBT_PROVISION = "DP", "Doubtful-debt Provision"
    INTER_BRANCH_TRANSFER = "IB", "Inter-branch Transfer"
    HELD_RECEIPT = "HF", "Receipt Held for Another Branch"
    RECHARGE = "RG", "Inter-branch Recharge"
    PETTY_CASH_RETURN = "PB", "Petty Cash Returned to Bank"
    PAYER_PAYMENT = "PA", "Payment Split Across a Payer's Customers"

# Define Account Type values.
class AccountType(models.TextChoices):
    """The five roots of double-entry accounting.

    Every account hangs under exactly one of these. The type fixes where the
    account lands on the financial statements and (together with ``is_contra``) its
    natural :class:`NormalBalance`:

    ASSET / EXPENSE      -> normally **debit** balances.
    LIABILITY / EQUITY / INCOME -> normally **credit** balances.

    A *contra* account (e.g. accumulated depreciation under ASSET, or sales returns
    under INCOME) keeps its parent's type but carries the opposite normal balance;
    that is modelled with the ``is_contra`` flag rather than a sixth pseudo-type, so
    it still rolls up cleanly into its statement section.
    """
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


ACCOUNT_TYPE_BY_CODE_PREFIX = {
    "1": AccountType.ASSET,
    "2": AccountType.LIABILITY,
    "3": AccountType.EQUITY,
    "4": AccountType.INCOME,
    "5": AccountType.EXPENSE,
}
ACCOUNT_CODE_LENGTH = 4


def account_type_from_code(code):
    """Return the canonical account type encoded by a Chart-of-Accounts code."""
    value = str(code).strip()
    return ACCOUNT_TYPE_BY_CODE_PREFIX.get(value[:1])


# Group behavior for Normal Balance.
class NormalBalance(models.TextChoices):
    """The side on which an account normally carries its balance."""
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


# Group behavior for Fee Applies To.
class FeeAppliesTo(models.TextChoices):
    """Who a :class:`vs_finance.models.FeeStructure` bills.

    This is a *generic* platform - a fee structure is not tied to a school term.
    It classifies the counterparty type the template charges: a client/customer
    (e.g. a school's students/payers), a vendor, a staff member, or a general
    template not bound to any counterparty type. Only ``CUSTOMER`` structures can
    currently generate AR invoices.
    """
    CUSTOMER = "CUSTOMER", "Customer"
    VENDOR = "VENDOR", "Vendor"
    STAFF = "STAFF", "Staff"
    GENERAL = "GENERAL", "General"


class ChargeKind(models.TextChoices):
    """What a fee item or invoice line bills: a charge earned, or money held for the customer.

    A ``CHARGE`` is income: it credits its revenue account, or the deferred-income
    liability while its service period has not begun. A ``DEPOSIT`` is refundable
    money the entity holds (a caution deposit): it credits the deposits-held
    liability and is never revenue, is kept per customer as a
    :class:`~vs_finance.models.CustomerDeposit`, and leaves that liability only
    when it is returned, set against unpaid bills, or forfeited as unclaimed.
    """
    CHARGE = "CHARGE", "Charge"
    DEPOSIT = "DEPOSIT", "Refundable deposit"


class RevenueRecognitionMethod(models.TextChoices):
    """How an invoice line billed ahead of its service period becomes revenue.

    Both apply only to a line whose service period starts after the invoice date;
    a line with no service period, or one whose period has already begun, is
    revenue on the invoice date. There is deliberately no "on billing" choice.

    ``SPREAD_MONTHLY`` recognises an equal share in each calendar month the service
    period touches, in whole kobo, with the remainder in the last month.
    ``AT_PERIOD_START`` recognises all of it in the month the service period starts.
    """
    SPREAD_MONTHLY = "SPREAD_MONTHLY", "Spread evenly over each month of the service period"
    AT_PERIOD_START = "AT_PERIOD_START", "All in the month the service period starts"


class DeferredIncomeStatus(models.TextChoices):
    """Where one month's share of deferred income stands."""
    PENDING = "PENDING", "Waiting to be released"
    RELEASED = "RELEASED", "Released to revenue"
    CANCELLED = "CANCELLED", "Cancelled"


class DepositStatus(models.TextChoices):
    """Where a customer's refundable deposit stands."""
    HELD = "HELD", "Held"
    RELEASED = "RELEASED", "Returned or set against bills"
    FORFEITED = "FORFEITED", "Forfeited as unclaimed"
    CANCELLED = "CANCELLED", "Cancelled with its invoice"


#: Default natural balance for each account root (before any contra flip).
NORMAL_BALANCE_BY_TYPE = {
    AccountType.ASSET: NormalBalance.DEBIT,
    AccountType.EXPENSE: NormalBalance.DEBIT,
    AccountType.LIABILITY: NormalBalance.CREDIT,
    AccountType.EQUITY: NormalBalance.CREDIT,
    AccountType.INCOME: NormalBalance.CREDIT,
}


# Group behavior for Journal Source.
class JournalSource(models.TextChoices):
    """Where a journal entry originated - for filtering and audit, not for posting logic.

    MANUAL entries are typed by a person; the rest are raised by sub-ledgers and
    automated processes (AR/AP postings, bank reconciliation, period-close accruals
    and depreciation, opening balances, FX revaluation).
    """
    MANUAL = "MANUAL", "Manual"
    SALES = "SALES", "Sales / AR"
    PURCHASE = "PURCHASE", "Purchase / AP"
    BANK = "BANK", "Bank / Cash"
    PAYROLL = "PAYROLL", "Payroll"
    CLOSING = "CLOSING", "Period Close"
    OPENING = "OPENING", "Opening Balance"
    FX = "FX", "FX Revaluation"
    SYSTEM = "SYSTEM", "System"
    TAX = "TAX", "Tax return"


# Group behavior for Invoice Source.
class InvoiceSource(models.TextChoices):
    """What generated an invoice - keeps the AR core domain-neutral.

    The invoice model is generic; ``source`` records the originating mechanism so a
    school-fee run, a subscription engine or an API caller can all emit the *same*
    generic :class:`~vs_finance.models.Invoice` without the ledger knowing about any
    of them. Student/fee concepts live only in the adapter that sets ``FEE_BILLING``.
    """
    MANUAL = "MANUAL", "Manual"
    FEE_BILLING = "FEE_BILLING", "Fee Billing"
    SUBSCRIPTION = "SUBSCRIPTION", "Subscription"
    API = "API", "API"
    OPENING = "OPENING", "Opening Balance"


# Define Invoice Payment Status values.
class InvoicePaymentStatus(models.TextChoices):
    """How much of an invoice has been settled - distinct from its document status.

    Document ``status`` (DRAFT→POSTED→…) tracks the *ledger* lifecycle; this tracks
    *cash* against the invoice and is derived from amount paid vs total.
    """
    UNPAID = "UNPAID", "Unpaid"
    PARTIAL = "PARTIAL", "Partially Paid"
    PAID = "PAID", "Paid"


# Define Credit Note Kind values.
class CreditNoteKind(models.TextChoices):
    """Direction of a credit/debit note against a customer's receivable.

    CREDIT reduces what the customer owes (a sales return, allowance or correction:
    ``Dr revenue/returns + Dr output tax, Cr AR``); it may be *applied* to specific
    invoices like a non-cash payment. DEBIT increases what the customer owes (an extra
    charge or under-bill correction: ``Dr AR, Cr revenue + Cr output tax``) - a
    supplementary invoice, so it is never allocated to reduce another invoice.
    """
    CREDIT = "CREDIT", "Credit note"
    DEBIT = "DEBIT", "Debit note"


# Group behavior for Payment Plan Frequency.
class PaymentPlanFrequency(models.TextChoices):
    """Spacing between installments in a payment plan (drives each due date)."""
    WEEKLY = "WEEKLY", "Weekly"
    FORTNIGHTLY = "FORTNIGHTLY", "Fortnightly (every 2 weeks)"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"


# Define Payment Plan Status values.
class PaymentPlanStatus(models.TextChoices):
    """Lifecycle of an installment payment plan (a scheduling overlay, never posted).

    DRAFT      -> schedule being built; editable.
    ACTIVE     -> committed; installments are live and tracked against settlement.
    COMPLETED  -> every installment fully settled.
    CANCELLED  -> abandoned; no longer tracked.
    """
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


# Define Installment Status values.
class InstallmentStatus(models.TextChoices):
    """Settlement state of a single installment, derived from amount settled vs due."""
    PENDING = "PENDING", "Pending"
    PARTIAL = "PARTIAL", "Partially settled"
    PAID = "PAID", "Settled"


# Define Concession Kind values.
class ConcessionKind(models.TextChoices):
    """A non-cash reduction of a receivable granted to a customer.

    DISCOUNT    -> commercial/early-settlement price reduction.
    WAIVER      -> charge forgiven (e.g. a penalty or fee dropped).
    SCHOLARSHIP -> a granted allowance against billed amounts (domain-neutral name for
                   a bursary/scholarship in a school tenant).

    All three post the same way - ``Dr discounts & allowances, Cr AR control`` - and
    reduce the invoice's balance via :attr:`Invoice.amount_credited`; ``kind`` is a
    reporting tag, not a different posting.
    """
    DISCOUNT = "DISCOUNT", "Discount"
    WAIVER = "WAIVER", "Waiver"
    SCHOLARSHIP = "SCHOLARSHIP", "Scholarship / bursary"


# Group behavior for Dunning Channel.
class DunningChannel(models.TextChoices):
    """How a dunning reminder is delivered (operational detail; vs_finance only records it).

    vs_finance does not itself send email/SMS - it tracks the *intent* and outcome; an
    outer service (notifications) reads PENDING notices and dispatches them.
    """
    EMAIL = "EMAIL", "Email"
    IN_APP = "IN_APP", "In-app"


# Define Dunning Notice Status values.
class DunningNoticeStatus(models.TextChoices):
    """Lifecycle of a single dunning notice (a communications overlay, never posted).

    PENDING   -> generated, awaiting dispatch.
    SENT      -> dispatched to the customer.
    RESOLVED  -> the underlying invoice was settled (or written off) after the notice.
    CANCELLED -> withdrawn before sending (e.g. a payment arrived, or a dispute opened).
    """
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    RESOLVED = "RESOLVED", "Resolved"
    CANCELLED = "CANCELLED", "Cancelled"


# Define which customer-facing document a delivery carried.
class FinanceDeliveryDocument(models.TextChoices):
    """The kinds of finance document that can be emailed to a customer.

    STATEMENT is not a stored document: it is a report over a customer and a date
    range, so its delivery row carries the period rather than a document id.
    """
    INVOICE = "INVOICE", "Invoice"
    RECEIPT = "RECEIPT", "Receipt"
    STATEMENT = "STATEMENT", "Statement of account"


# Define Finance Delivery Status values.
class FinanceDeliveryStatus(models.TextChoices):
    """Outcome of one attempt to email a customer document.

    PENDING -> handed to vs_notifications, no outcome yet.
    SENT    -> every notification for the attempt reported success.
    FAILED  -> at least one reported failure; the attempt can be retried.

    There is deliberately no AWAITING_APPROVAL state (unlike a purchase order):
    a finance document is emailed on demand, with nothing to wait for.
    """
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


# Define how a customer document email was requested.
class FinanceDeliverySource(models.TextChoices):
    """Why the delivery happened, so history distinguishes the automatic copy sent
    on posting from a re-send somebody asked for."""
    AUTOMATIC = "AUTOMATIC", "Automatic on posting"
    MANUAL = "MANUAL", "Manual send"
    RETRY = "RETRY", "Retry"


# Define Payment Method values.
class PaymentMethod(models.TextChoices):
    """How a customer receipt was tendered (operational detail, not posting logic)."""
    CASH = "CASH", "Cash"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    CARD = "CARD", "Card"
    CHEQUE = "CHEQUE", "Cheque"
    ONLINE = "ONLINE", "Online / Gateway"
    OTHER = "OTHER", "Other"


#: The method of a customer receipt that carries credit moved from another customer
#: by an approved transfer (:class:`~vs_finance.models.CustomerCreditTransfer`). No
#: money arrived: the receipt holds credit the source customer already had. Only a
#: customer receipt takes it, so it extends :class:`PaymentMethod` for that one field.
CREDIT_TRANSFER_METHOD = "CREDIT_TRANSFER"
RECEIPT_METHOD_CHOICES = [*PaymentMethod.choices, (CREDIT_TRANSFER_METHOD, "Customer credit transfer")]


# --------------------------------------------------------------------------- #
# Banking, expenses, payroll, budget, fixed assets, period close     #          
# --------------------------------------------------------------------------- #

class BankTransactionDirection(models.TextChoices):
    """Which way money moves on a :class:`~vs_finance.models.BankTransaction`.

    IN  -> money arrives in the bank account (Dr bank, Cr the counter-account).
    OUT -> money leaves it (Dr the counter-account, Cr bank).
    """
    IN = "IN", "Money in"
    OUT = "OUT", "Money out"


class PettyCashReturnKind(models.TextChoices):
    """Why cash in a petty cash fund goes back to the bank.

    REDUCE -> the float is cut and the cash above it is banked; the fund carries on.
    CLOSE  -> every note in the tin is banked and the fund stops.
    """
    REDUCE = "REDUCE", "Reduce the float"
    CLOSE = "CLOSE", "Close the fund"


# Define Bank Line Status values.
class BankLineStatus(models.TextChoices):
    """Reconciliation state of an imported bank-statement line.

    UNMATCHED -> not yet paired with a ledger movement.
    MATCHED   -> reconciled to a GL bank-account journal line.
    IGNORED   -> intentionally excluded (duplicate, opening line, etc.).
    """
    UNMATCHED = "UNMATCHED", "Unmatched"
    MATCHED = "MATCHED", "Matched"
    IGNORED = "IGNORED", "Ignored"


# Group behavior for Bank Match Source.
class BankMatchSource(models.TextChoices):
    """How a statement line came to be matched."""
    AUTO = "AUTO", "Auto"
    MANUAL = "MANUAL", "Manual"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"


# Define Bank Statement Status values.
class BankStatementStatus(models.TextChoices):
    """Lifecycle of an imported bank statement (a batch of lines for a period)."""
    UPLOADED = "UPLOADED", "Uploaded"
    RECONCILED = "RECONCILED", "Reconciled"


# Define Bank Recon Status values.
class BankReconStatus(models.TextChoices):
    """Outcome of a reconciliation run."""
    BALANCED = "BALANCED", "Balanced"
    OUT_OF_BALANCE = "OUT_OF_BALANCE", "Out of balance"


# Define Payroll Run Status values.
class PayrollRunStatus(models.TextChoices):
    """Lifecycle of a payroll run (a batch of employee pay lines)."""
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted (accrued)"
    PAID = "PAID", "Paid (disbursed)"
    CANCELLED = "CANCELLED", "Cancelled"


# Define Salary Component Kind values.
class SalaryComponentKind(models.TextChoices):
    """Whether a salary-structure component adds to pay or is withheld from it."""
    EARNING = "EARNING", "Earning"
    DEDUCTION = "DEDUCTION", "Deduction"


# Define Salary Calc Method values.
class SalaryCalcMethod(models.TextChoices):
    """How a salary component's amount is derived from an employee's gross."""
    FIXED = "FIXED", "Fixed amount"
    PERCENT_OF_GROSS = "PERCENT_OF_GROSS", "Percent of gross"
    PERCENT_OF_BASIC = "PERCENT_OF_BASIC", "Percent of basic"


# Define Statutory Type values.
class StatutoryType(models.TextChoices):
    """Which statutory liability a deduction feeds - routes the GL credit and the return.

    Earnings are always ``NONE``; deductions must be ``PAYE`` or ``PENSION`` so the
    accrual journal stays balanced (``net = gross - paye - pension``).
    """
    NONE = "NONE", "None"
    PAYE = "PAYE", "PAYE"
    PENSION = "PENSION", "Pension"


class PayeMethod(models.TextChoices):
    """Where a tenant's PAYE and employee pension figures come from.

    ``COMPUTED`` works PAYE out from the national tax table of the payroll
    month's tax year and the employee pension from the tenant's statutory rate.
    ``SUPPLIED`` takes both from the salary structure or the figures typed on the
    roster, for a tenant whose payroll is worked out elsewhere.
    """
    COMPUTED = "COMPUTED", "Computed from the national tax table"
    SUPPLIED = "SUPPLIED", "Taken from the salary structure or roster"


class PayeSource(models.TextChoices):
    """How one payroll line's PAYE figure was arrived at.

    ``COMPUTED`` and ``SUPPLIED`` are the two ways a tenant's payroll works
    (:class:`PayeMethod`) and read in the same words, so a line and the setting
    that produced it never describe one thing two ways. ``OVERRIDE`` is a figure
    fixed on one employee's salary, ``MANUAL`` one typed on a hand-raised run.
    """
    COMPUTED = "COMPUTED", PayeMethod.COMPUTED.label
    OVERRIDE = "OVERRIDE", "Overridden on the employee's salary"
    SUPPLIED = "SUPPLIED", PayeMethod.SUPPLIED.label
    MANUAL = "MANUAL", "Typed on a hand-raised run"


class PayrollItemKind(models.TextChoices):
    """Whether a payroll line item is withheld from the employee or paid by the employer."""
    DEDUCTION = "DEDUCTION", "Employee deduction"
    EMPLOYER = "EMPLOYER", "Employer contribution"


class PayrollItemCode(models.TextChoices):
    """What a payroll line item is, which decides the accounts it posts to."""
    PAYE = "PAYE", "PAYE"
    PENSION = "PENSION", "Employee pension"
    NHF = "NHF", "National Housing Fund"
    VOLUNTARY = "VOLUNTARY", "Voluntary deduction"
    EMPLOYER_PENSION = "EMPLOYER_PENSION", "Employer pension"
    NSITF = "NSITF", "NSITF employee compensation"
    ITF = "ITF", "ITF training levy"


class PayeReliefKind(models.TextChoices):
    """How a relief rule in a national tax table reduces taxable income.

    ``CONTRIBUTION`` deducts what the employee actually contributed of the
    ``basis`` kind (pension, NHF). ``PERCENT_CAPPED`` deducts ``rate_bps`` of the
    basis, no less than ``floor_amount`` and no more than ``cap_amount`` a year.
    ``FIXED`` deducts ``cap_amount`` a year.
    """
    CONTRIBUTION = "CONTRIBUTION", "Contribution actually made"
    PERCENT_CAPPED = "PERCENT_CAPPED", "Percentage of a basis, floored and capped"
    FIXED = "FIXED", "Fixed amount a year"


class PayeReliefBasis(models.TextChoices):
    """The figure a relief rule is measured against."""
    NONE = "NONE", "None"
    PENSION = "PENSION", "Employee pension contributions"
    NHF = "NHF", "National Housing Fund contributions"
    ANNUAL_RENT = "ANNUAL_RENT", "Annual rent the employee pays"
    ANNUAL_GROSS = "ANNUAL_GROSS", "Annual gross income"


class PayslipEmailStatus(models.TextChoices):
    """Where a payslip's email stands."""
    NOT_REQUESTED = "NOT_REQUESTED", "Not emailed (switched off)"
    PENDING = "PENDING", "Waiting to be sent"
    QUEUED = "QUEUED", "Handed to notifications"
    NO_ADDRESS = "NO_ADDRESS", "No email address on file"
    FAILED = "FAILED", "Could not be queued"


# Define Budget Status values.
class BudgetStatus(models.TextChoices):
    """Lifecycle of a budget; approval locks the figures so actuals can't be re-planned.

    Two states only: a DRAFT budget is editable; APPROVED locks it (see
    :attr:`Budget.is_locked`). There is no separate LOCKED state - approval *is* the lock.
    """
    DRAFT = "DRAFT", "Draft"
    APPROVED = "APPROVED", "Approved"


# Define Depreciation Method values.
class DepreciationMethod(models.TextChoices):
    """Depreciation method for a fixed asset."""
    STRAIGHT_LINE = "STRAIGHT_LINE", "Straight line"
    DECLINING_BALANCE = "DECLINING_BALANCE", "Declining balance"


# Define Asset Status values.
class AssetStatus(models.TextChoices):
    """Lifecycle of a fixed asset in the register."""
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    FULLY_DEPRECIATED = "FULLY_DEPRECIATED", "Fully depreciated"
    DISPOSED = "DISPOSED", "Disposed"


# Group behavior for Asset Category.
class AssetCategory(models.TextChoices):
    """Broad register category for a fixed asset (drives the list filter/column)."""
    VEHICLES = "VEHICLES", "Vehicles"
    BUILDINGS = "BUILDINGS", "Buildings"
    PLANT_MACHINERY = "PLANT_MACHINERY", "Plant & machinery"
    IT_EQUIPMENT = "IT_EQUIPMENT", "IT equipment"
    FURNITURE = "FURNITURE", "Furniture & fittings"
    EQUIPMENT = "EQUIPMENT", "Equipment"
    OTHER = "OTHER", "Other"


# Define Finance Audit Action values.
class FinanceAuditAction(models.TextChoices):
    """Auditable finance actions recorded in the in-app, append-only audit log.

    The ledger itself (immutable posted/reversed journals + period locks) is the
    primary financial audit trail; this enum names the *actions around* it that the
    journals can't capture on their own - who pressed post, rejected attempts, period
    state changes and master-data edits.
    """
    JOURNAL_POSTED = "JOURNAL_POSTED", "Journal posted"
    JOURNAL_REVERSED = "JOURNAL_REVERSED", "Journal reversed"
    JOURNAL_POST_REJECTED = "JOURNAL_POST_REJECTED", "Journal posting rejected"
    INVOICE_POSTED = "INVOICE_POSTED", "Invoice posted"
    INVOICE_CANCELLED = "INVOICE_CANCELLED", "Invoice cancelled"
    INVOICE_REVERSED = "INVOICE_REVERSED", "Invoice reversed"
    INVOICE_WRITTEN_OFF = "INVOICE_WRITTEN_OFF", "Invoice written off (bad debt)"
    INVOICE_PAY_LINK_REVOKED = (
        "INVOICE_PAY_LINK_REVOKED", "Invoice pay link revoked",
    )
    PAYMENT_POSTED = "PAYMENT_POSTED", "Payment posted"
    PAYMENT_ALLOCATED = "PAYMENT_ALLOCATED", "Payment allocated"
    PAYMENT_REVERSED = "PAYMENT_REVERSED", "Payment reversed"
    CREDIT_NOTE_POSTED = "CREDIT_NOTE_POSTED", "Credit note posted"
    CREDIT_NOTE_ALLOCATED = "CREDIT_NOTE_ALLOCATED", "Credit note allocated"
    DEBIT_NOTE_POSTED = "DEBIT_NOTE_POSTED", "Debit note posted"
    CREDIT_NOTE_REVERSED = "CREDIT_NOTE_REVERSED", "Credit/debit note reversed"
    REFUND_POSTED = "REFUND_POSTED", "Customer refund posted"
    REFUND_REVERSED = "REFUND_REVERSED", "Customer refund reversed"
    PAYMENT_PLAN_ACTIVATED = "PAYMENT_PLAN_ACTIVATED", "Installment plan activated"
    PAYMENT_PLAN_COMPLETED = "PAYMENT_PLAN_COMPLETED", "Installment plan completed"
    PAYMENT_PLAN_CANCELLED = "PAYMENT_PLAN_CANCELLED", "Installment plan cancelled"
    CONCESSION_POSTED = "CONCESSION_POSTED", "Concession / discount / waiver posted"
    CONCESSION_REVERSED = "CONCESSION_REVERSED", "Concession / discount / waiver reversed"
    CUSTOMER_UPDATED = "CUSTOMER_UPDATED", "Customer updated"
    CUSTOMER_DEACTIVATED = "CUSTOMER_DEACTIVATED", "Customer deactivated"
    CUSTOMER_REACTIVATED = "CUSTOMER_REACTIVATED", "Customer reactivated"
    CUSTOMER_OPENING_POSTED = "CUSTOMER_OPENING_POSTED", "Customer opening invoice posted"
    CREDIT_TRANSFER_POSTED = "CREDIT_TRANSFER_POSTED", "Customer credit transfer posted"
    CREDIT_TRANSFER_REVERSED = "CREDIT_TRANSFER_REVERSED", "Customer credit transfer reversed"
    DEFERRED_INCOME_RELEASED = "DEFERRED_INCOME_RELEASED", "Deferred income released"
    DEFERRED_RELEASE_REVERSED = "DEFERRED_RELEASE_REVERSED", "Deferred income release reversed"
    PROVISION_POSTED = "PROVISION_POSTED", "Doubtful-debt provision posted"
    WRITE_OFF_RECOVERED = "WRITE_OFF_RECOVERED", "Written-off debt recovered"
    DEPOSIT_RELEASED = "DEPOSIT_RELEASED", "Customer deposit released"
    DEPOSITS_FORFEITED = "DEPOSITS_FORFEITED", "Unclaimed deposits forfeited"
    RECEIPT_PARKED_AS_CREDIT = "RECEIPT_PARKED_AS_CREDIT", "Receipt parked as customer credit"
    DUNNING_RUN_GENERATED = "DUNNING_RUN_GENERATED", "Dunning run generated"
    DUNNING_NOTICE_SENT = "DUNNING_NOTICE_SENT", "Dunning notice marked sent"
    DUNNING_NOTICE_CANCELLED = "DUNNING_NOTICE_CANCELLED", "Dunning notice cancelled"
    PERIOD_CLOSED = "PERIOD_CLOSED", "Period closed"
    PERIOD_REOPENED = "PERIOD_REOPENED", "Period re-opened"
    ACCOUNT_CREATED = "ACCOUNT_CREATED", "Account created"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED", "Account updated"
    FINANCE_SETTINGS_UPDATED = "FINANCE_SETTINGS_UPDATED", "Finance settings updated"
    FINANCE_DOCUMENT_SETTINGS_UPDATED = (
        "FIN_DOCUMENT_SETTINGS_UPDATED", "Finance document settings updated"
    )
    FINANCE_BANKING_SETTINGS_UPDATED = (
        "FIN_BANK_SETTINGS_UPDATED", "Finance banking settings updated"
    )
    FINANCE_CALENDAR_SETTINGS_UPDATED = (
        "FIN_CALENDAR_SETTINGS_UPDATED", "Finance calendar settings updated"
    )
    FINANCE_RECEIVABLES_SETTINGS_UPDATED = (
        "FIN_RECEIVABLES_SETTINGS_UPDATED", "Finance receivables settings updated"
    )
    PROCUREMENT_SETTINGS_UPDATED = "PROCUREMENT_SETTINGS_UPDATED", "Procurement settings updated"
    # Procure-to-Pay. The vendor/PO/GRN documents live in vs_procurement,
    # but their audit vocabulary belongs to finance's authoritative log (finance does
    # not import procurement - these are just string constants).
    REQUISITION_APPROVED = "REQUISITION_APPROVED", "Requisition approved"
    # A spend document that nobody could approve, released without a vote by a
    # permissioned human. This is the row an auditor looks for: it is never written
    # by an ordinary approval, only by the dedicated, reasoned override.
    SPEND_APPROVAL_OVERRIDDEN = (
        "SPEND_APPROVAL_OVERRIDDEN", "Spend approval released by override (no review)"
    )
    RFQ_ISSUED = "RFQ_ISSUED", "Request for quotation issued"
    RFQ_CANCELLED = "RFQ_CANCELLED", "Request for quotation cancelled"
    RFQ_CLOSED = "RFQ_CLOSED", "Request for quotation closed without award"
    QUOTATION_SUBMITTED = "QUOTATION_SUBMITTED", "Vendor quotation submitted"
    QUOTATION_AWARDED = "QUOTATION_AWARDED", "Vendor quotation awarded → PO"
    QUOTATION_REJECTED = "QUOTATION_REJECTED", "Vendor quotation rejected"
    VENDOR_CONTRACT_ACTIVATED = "VENDOR_CONTRACT_ACTIVATED", "Vendor contract activated"
    VENDOR_CONTRACT_RENEWED = "VENDOR_CONTRACT_RENEWED", "Vendor contract renewed"
    VENDOR_CONTRACT_TERMINATED = "VENDOR_CONTRACT_TERMINATED", "Vendor contract terminated"
    CONTRACT_MILESTONE_COMPLETED = "CONTRACT_MILESTONE_COMPLETED", "Contract milestone completed"
    PURCHASE_ORDER_APPROVED = "PURCHASE_ORDER_APPROVED", "Purchase order approved"
    PURCHASE_ORDER_CANCELLED = "PURCHASE_ORDER_CANCELLED", "Purchase order cancelled"
    PURCHASE_ORDER_EMAIL_SCHEDULED = "PO_EMAIL_SCHEDULED", "Purchase order email scheduled"
    PURCHASE_ORDER_EMAIL_QUEUED = "PO_EMAIL_QUEUED", "Purchase order email queued"
    PURCHASE_ORDER_EMAIL_SENT = "PO_EMAIL_SENT", "Purchase order email sent"
    PURCHASE_ORDER_EMAIL_FAILED = "PO_EMAIL_FAILED", "Purchase order email failed"
    PURCHASE_ORDER_EMAIL_CANCELLED = "PO_EMAIL_CANCELLED", "Purchase order email cancelled"
    DOCUMENT_EMAIL_QUEUED = "DOC_EMAIL_QUEUED", "Customer document email queued"
    DOCUMENT_EMAIL_SENT = "DOC_EMAIL_SENT", "Customer document email sent"
    DOCUMENT_EMAIL_FAILED = "DOC_EMAIL_FAILED", "Customer document email failed"
    GRN_POSTED = "GRN_POSTED", "Goods receipt posted"
    GRN_POST_REJECTED = "GRN_POST_REJECTED", "Goods receipt posting rejected"
    VENDOR_INVOICE_MATCHED = "VENDOR_INVOICE_MATCHED", "Vendor invoice matched"
    VENDOR_INVOICE_APPROVED = "VENDOR_INVOICE_APPROVED", "Vendor invoice approved (workflow)"
    VENDOR_INVOICE_POSTED = "VENDOR_INVOICE_POSTED", "Vendor invoice posted"
    VENDOR_INVOICE_POST_REJECTED = "VENDOR_INVOICE_POST_REJECTED", "Vendor invoice posting rejected"
    VENDOR_PAYMENT_POSTED = "VENDOR_PAYMENT_POSTED", "Vendor payment posted"
    VENDOR_PAYMENT_POST_REJECTED = "VENDOR_PAYMENT_POST_REJECTED", "Vendor payment posting rejected"
    VENDOR_PAYMENT_ALLOCATED = "VENDOR_PAYMENT_ALLOCATED", "Vendor payment allocated"
    VENDOR_INVOICE_VOIDED = "VENDOR_INVOICE_VOIDED", "Vendor invoice voided"
    VENDOR_CREDIT_NOTE_APPROVED = "VENDOR_CREDIT_NOTE_APPROVED", "Vendor credit note approved (workflow)"
    VENDOR_CREDIT_NOTE_POSTED = "VENDOR_CREDIT_NOTE_POSTED", "Vendor credit note posted"
    VENDOR_CREDIT_NOTE_ALLOCATED = "VENDOR_CREDIT_NOTE_ALLOCATED", "Vendor credit note allocated"
    VENDOR_CREDIT_NOTE_VOIDED = "VENDOR_CREDIT_NOTE_VOIDED", "Vendor credit note voided"
    GOODS_RETURNED = "GOODS_RETURNED", "Goods returned to vendor"
    VENDOR_OPENING_BILL_POSTED = "VENDOR_OPENING_BILL_POSTED", "Vendor opening bill posted"
    STOCK_RECEIVED = "STOCK_RECEIVED", "Stock received (perpetual inventory)"
    STOCK_ISSUED = "STOCK_ISSUED", "Stock issued"
    STOCK_ISSUE_REJECTED = "STOCK_ISSUE_REJECTED", "Stock issue rejected"
    STOCK_ADJUSTED = "STOCK_ADJUSTED", "Stock adjusted"
    STOCK_ADJUST_REJECTED = "STOCK_ADJUST_REJECTED", "Stock adjustment rejected"
    # Banking, expenses, payroll, budget, fixed assets, period close.          
    BANK_STATEMENT_CORRECTED = "BANK_STATEMENT_CORRECTED", "Bank statement corrected"
    BANK_RECONCILED = "BANK_RECONCILED", "Bank statement reconciled"
    BANK_CHARGE_POSTED = "BANK_CHARGE_POSTED", "Bank charge posted"
    BANK_TRANSACTION_POSTED = "BANK_TRANSACTION_POSTED", "Bank transaction posted"
    BANK_TRANSACTION_VOIDED = "BANK_TRANSACTION_VOIDED", "Bank transaction voided"
    BANK_TRANSFER_POSTED = "BANK_TRANSFER_POSTED", "Transfer between own accounts posted"
    BANK_TRANSFER_VOIDED = "BANK_TRANSFER_VOIDED", "Transfer between own accounts voided"
    BANK_TRANSACTION_EDITED = "BANK_TRANSACTION_EDITED", "Draft bank transaction corrected"
    BANK_TRANSACTION_CANCELLED = "BANK_TRANSACTION_CANCELLED", "Draft bank transaction cancelled"
    BANK_TRANSFER_EDITED = "BANK_TRANSFER_EDITED", "Draft transfer between own accounts corrected"
    BANK_TRANSFER_CANCELLED = "BANK_TRANSFER_CANCELLED", "Draft transfer between own accounts cancelled"
    JOURNAL_EDITED = "JOURNAL_EDITED", "Draft direct entry corrected"
    CREDIT_NOTE_EDITED = "CREDIT_NOTE_EDITED", "Draft credit or debit note corrected"
    CONCESSION_EDITED = "CONCESSION_EDITED", "Draft concession corrected"
    EXPENSE_CLAIM_POSTED = "EXPENSE_CLAIM_POSTED", "Expense claim posted"
    EXPENSE_CLAIM_POST_REJECTED = "EXPENSE_CLAIM_POST_REJECTED", "Expense claim posting rejected"
    EXPENSE_CLAIM_SETTLED = "EXPENSE_CLAIM_SETTLED", "Expense claim settled"
    EXPENSE_CLAIM_VOIDED = "EXPENSE_CLAIM_VOIDED", "Expense claim voided"
    PETTY_CASH_ESTABLISHED = "PETTY_CASH_ESTABLISHED", "Petty cash fund established / topped up"
    PETTY_CASH_VOUCHER_POSTED = "PETTY_CASH_VOUCHER_POSTED", "Petty cash voucher posted"
    PETTY_CASH_VOUCHER_REJECTED = "PETTY_CASH_VOUCHER_REJECTED", "Petty cash voucher rejected"
    PETTY_CASH_VOUCHER_VOIDED = "PETTY_CASH_VOUCHER_VOIDED", "Petty cash voucher voided"
    PETTY_CASH_REPLENISHED = "PETTY_CASH_REPLENISHED", "Petty cash fund replenished"
    PAYROLL_POSTED = "PAYROLL_POSTED", "Payroll run posted"
    PAYROLL_POST_REJECTED = "PAYROLL_POST_REJECTED", "Payroll run posting rejected"
    PAYROLL_PAID = "PAYROLL_PAID", "Payroll run disbursed"
    PAYROLL_CANCELLED = "PAYROLL_CANCELLED", "Payroll run cancelled / voided"
    BUDGET_APPROVED = "BUDGET_APPROVED", "Budget approved"
    BUDGET_DELETED = "BUDGET_DELETED", "Budget deleted"
    ASSET_ACQUIRED = "ASSET_ACQUIRED", "Fixed asset acquired"
    DEPRECIATION_POSTED = "DEPRECIATION_POSTED", "Depreciation posted"
    ASSET_DISPOSED = "ASSET_DISPOSED", "Fixed asset disposed"
    PERIOD_LOCKED = "PERIOD_LOCKED", "Period locked"
    FISCAL_YEAR_OPENED = "FISCAL_YEAR_OPENED", "Fiscal year opened"
    FISCAL_YEAR_CLOSED = "FISCAL_YEAR_CLOSED", "Fiscal year closed"
    FISCAL_YEAR_REOPENED = "FISCAL_YEAR_REOPENED", "Fiscal year re-opened"
    FISCAL_CALENDAR_WARNED = (
        "FISCAL_CALENDAR_WARNED", "Finance staff warned the fiscal calendar is running out"
    )
    TAX_FILING_PREPARED = "TAX_FILING_PREPARED", "Tax filing prepared"
    TAX_FILING_FILED = "TAX_FILING_FILED", "Tax filing submitted to authority"
    TAX_FILING_UNFILED = "TAX_FILING_UNFILED", "Tax filing un-filed (reverted to draft)"
    TAX_FILING_PAID = "TAX_FILING_PAID", "Tax filing paid / remitted"
    TAX_FILING_REJECTED = "TAX_FILING_REJECTED", "Tax filing action rejected"
    TAX_REMITTANCE_REVERSED = "TAX_REMITTANCE_REVERSED", "Tax remittance reversed"
    PAYROLL_SETTINGS_UPDATED = "FIN_PAYROLL_SETTINGS_UPDATED", "Finance payroll settings updated"
    SALARY_CREATED = "SALARY_CREATED", "Employee salary added"
    SALARY_CHANGED = "SALARY_CHANGED", "Employee salary changed"
    SALARY_DEACTIVATED = "SALARY_DEACTIVATED", "Employee salary deactivated"
    PAYE_OVERRIDE_CHANGED = "PAYE_OVERRIDE_CHANGED", "PAYE override set or cleared"
    SALARY_STRUCTURE_CHANGED = "SALARY_STRUCTURE_CHANGED", "Salary structure changed"
    PAYROLL_DEDUCTION_CHANGED = "PAYROLL_DEDUCTION_CHANGED", "Payroll deduction changed"
    PAYSLIPS_ISSUED = "PAYSLIPS_ISSUED", "Payslips issued"
    INTER_BRANCH_REQUESTED = "INTER_BRANCH_REQUESTED", "Inter-branch transfer requested"
    INTER_BRANCH_SENT = "INTER_BRANCH_SENT", "Inter-branch transfer sent"
    INTER_BRANCH_DECLINED = "INTER_BRANCH_DECLINED", "Inter-branch transfer request declined"
    INTER_BRANCH_CONFIRMED = "INTER_BRANCH_CONFIRMED", "Inter-branch transfer arrival confirmed"
    INTER_BRANCH_VOIDED = "INTER_BRANCH_VOIDED", "Inter-branch transfer voided"
    HELD_RECEIPT_POSTED = "HELD_RECEIPT_POSTED", "Receipt held for another branch"
    HELD_RECEIPT_VOIDED = "HELD_RECEIPT_VOIDED", "Receipt held for another branch voided"
    RECHARGE_POSTED = "RECHARGE_POSTED", "Shared cost recharged to other branches"
    RECHARGE_VOIDED = "RECHARGE_VOIDED", "Shared cost recharge voided"
    RECEIVABLE_TRANSFERRED = "RECEIVABLE_TRANSFERRED", "Open receivable moved to another branch"
    INCOME_GIVEN_BACK = "INCOME_GIVEN_BACK", "Income held at another branch given back"
    SHARED_COST_RULE_CHANGED = "SHARED_COST_RULE_CHANGED", "Shared cost rule changed"
    STOCK_TRANSFERRED = "STOCK_TRANSFERRED", "Stock moved between stores"
    PETTY_CASH_RETURN_POSTED = "PETTY_CASH_RETURN_POSTED", "Petty cash returned to the bank"
    PETTY_CASH_RETURN_VOIDED = "PETTY_CASH_RETURN_VOIDED", "Petty cash return voided"
    PETTY_CASH_FUND_CLOSED = "PETTY_CASH_FUND_CLOSED", "Petty cash fund closed"
    PETTY_CASH_FUND_REOPENED = "PETTY_CASH_FUND_REOPENED", "Petty cash fund reopened"
    PETTY_CASH_FUND_UPDATED = "PETTY_CASH_FUND_UPDATED", "Petty cash fund details changed"
    PETTY_CASH_VOUCHER_CANCELLED = "PETTY_CASH_VOUCHER_CANCELLED", "Draft petty cash voucher cancelled"
    PAYER_PAYMENT_POSTED = "PAYER_PAYMENT_POSTED", "Payment split across a payer's customers"
    PAYER_PAYMENT_VOIDED = "PAYER_PAYMENT_VOIDED", "Payer payment voided"
    PAYER_LINK_CHANGED = "PAYER_LINK_CHANGED", "Customers a payer pays for changed"
    FISCAL_YEAR_ARCHIVED = "FISCAL_YEAR_ARCHIVED", "Fiscal year archived"
    FISCAL_YEAR_UNARCHIVED = "FISCAL_YEAR_UNARCHIVED", "Fiscal year unarchived"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED", "Evidence file attached"
    ATTACHMENT_SUPERSEDED = "ATTACHMENT_SUPERSEDED", "Evidence file superseded"
    ATTACHMENT_REMOVED = "ATTACHMENT_REMOVED", "Evidence file removed from a draft"
    RETENTION_SETTINGS_UPDATED = "RETENTION_SETTINGS_UPDATED", "Record retention settings updated"


class InterBranchTransferKind(models.TextChoices):
    """What an inter-branch transfer moves between two branches.

    Only CASH and FORWARDED_RECEIPT move money between banks. The others move a
    balance and leave cash where it is; the owing branch settles later with a
    cash transfer the other way.

    INCOME_GIVEN_BACK is booked by a credit note or concession that cancels a
    moved bill's income another branch booked
    (:func:`vs_finance.inter_branch.book_income_given_back`), and is voided only
    with that document.

    BANK_SPLIT carries the difference between a branch's book balance on a
    shared bank account and the share it agreed to take when the account was
    split into branch accounts (:mod:`vs_finance.bank_splits`). It is never
    voided: the shared ledger it moved is retired, so the owing branch settles
    it with a cash transfer the other way.
    """
    CASH = "CASH", "Cash"
    FORWARDED_RECEIPT = "FORWARDED_RECEIPT", "Forwarded receipt"
    RECEIVABLE = "RECEIVABLE", "Receivable"
    RECHARGE = "RECHARGE", "Recharge"
    GOODS = "GOODS", "Goods"
    INCOME_GIVEN_BACK = "INCOME_GIVEN_BACK", "Income given back"
    BANK_SPLIT = "BANK_SPLIT", "Shared bank split"


class InterBranchLegRole(models.TextChoices):
    """Which side of an inter-branch transfer a leg books."""
    SENDING = "SENDING", "Sending branch"
    RECEIVING = "RECEIVING", "Receiving branch"


class ReceivableMoveItemKind(models.TextChoices):
    """What a receivable move carried to the new branch, one row per document."""
    INVOICE = "INVOICE", "Invoice"
    DEBIT_NOTE = "DEBIT_NOTE", "Debit note"
    RECEIPT_CREDIT = "RECEIPT_CREDIT", "Unapplied receipt"
    NOTE_CREDIT = "NOTE_CREDIT", "Unapplied credit note"


class SharedCostTreatment(models.TextChoices):
    """What happens to a cost one branch pays on behalf of others."""
    ABSORB = "ABSORB", "Absorbed by the paying branch"
    RECHARGE = "RECHARGE", "Recharged to the other branches"


class RechargeBasis(models.TextChoices):
    """How a recharged cost is split between branches."""
    COUNTS = "COUNTS", "Per-branch counts"
    PERCENTAGES = "PERCENTAGES", "Fixed percentages"


# Define Finance Audit Status values.
class FinanceAuditStatus(models.TextChoices):
    """Outcome of an audited action."""
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


# Define Tax Obligation Type values.
class TaxObligationType(models.TextChoices):
    """The statutory tax a remittance obligation covers."""
    VAT = "VAT", "Value Added Tax"
    WHT = "WHT", "Withholding Tax"
    PAYE = "PAYE", "Pay-As-You-Earn (employee income tax)"
    PENSION = "PENSION", "Pension contribution"
    NHF = "NHF", "National Housing Fund"
    NSITF = "NSITF", "NSITF employee compensation"
    ITF = "ITF", "ITF training levy"
    OTHER = "OTHER", "Other statutory levy"


# Group behavior for Tax Filing Frequency.
class TaxFilingFrequency(models.TextChoices):
    """How often a return falls due for an obligation."""
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    ANNUAL = "ANNUAL", "Annual"


class TaxTreatment(models.TextChoices):
    """How a supply is treated for VAT, carried on its tax code.

    Only a STANDARD code charges tax at its rate. A ZERO_RATED supply is taxable
    at nil, so the input tax spent making it stays recoverable; an EXEMPT supply
    is outside the tax, and charges none. Both carry a rate of zero, which the
    tax code enforces, so every reader that multiplies a line by its code's rate
    already puts nothing on the output tax account for them.
    """
    STANDARD = "STANDARD", "Standard rated"
    ZERO_RATED = "ZERO_RATED", "Zero rated"
    EXEMPT = "EXEMPT", "Exempt"


class TaxSourceRole(models.TextChoices):
    """Which of an obligation's accounts a declared source line sits on."""
    PAYABLE = "PAYABLE", "Tax payable"
    RECOVERABLE = "RECOVERABLE", "Recoverable input tax"


# Define Tax Filing Status values.
class TaxFilingStatus(models.TextChoices):
    """Lifecycle of a single tax return: prepared, filed with the authority, paid."""
    DRAFT = "DRAFT", "Draft / prepared"
    FILED = "FILED", "Filed with authority"
    PAID = "PAID", "Paid / remitted"
    CANCELLED = "CANCELLED", "Cancelled"


# Define I F R S Line values.
class IFRSLine(models.TextChoices):
    """IFRS-for-SMEs presentation lines a chart account rolls up to.

    The five double-entry roots (:class:`AccountType`) decide *where* an account
    lands on the statements; this finer classification decides *which statutory line*
    it presents on, so the Statement of Financial Position and Income Statement read
    the way FIRS / CAC filings expect rather than as a raw account list. An account
    with a blank ``ifrs_line`` falls back to a type-derived default (see
    :data:`DEFAULT_IFRS_LINE_BY_TYPE`), so the mapping degrades gracefully on a
    customised chart.
    """
    # Statement of Financial Position - non-current assets.
    PPE = "PPE", "Property, plant and equipment"
    INTANGIBLES = "INTANGIBLES", "Intangible assets"
    INVESTMENTS = "INVESTMENTS", "Investments"
    # Statement of Financial Position - current assets.
    INVENTORIES = "INVENTORIES", "Inventories"
    TRADE_RECEIVABLES = "TRADE_RECEIVABLES", "Trade and other receivables"
    CURRENT_TAX_ASSET = "CURRENT_TAX_ASSET", "Current tax assets"
    CASH = "CASH", "Cash and cash equivalents"
    OTHER_CURRENT_ASSETS = "OTHER_CURRENT_ASSETS", "Other current assets"
    # Statement of Financial Position - equity.
    SHARE_CAPITAL = "SHARE_CAPITAL", "Share capital"
    RETAINED_EARNINGS = "RETAINED_EARNINGS", "Retained earnings"
    OTHER_RESERVES = "OTHER_RESERVES", "Other reserves"
    # Statement of Financial Position - non-current liabilities.
    LONG_TERM_BORROWINGS = "LONG_TERM_BORROWINGS", "Long-term borrowings"
    # Statement of Financial Position - current liabilities.
    TRADE_PAYABLES = "TRADE_PAYABLES", "Trade and other payables"
    DEFERRED_INCOME = "DEFERRED_INCOME", "Deferred income"
    CURRENT_TAX_PAYABLE = "CURRENT_TAX_PAYABLE", "Current tax payable"
    EMPLOYEE_PAYABLES = "EMPLOYEE_PAYABLES", "Employee benefit obligations"
    SHORT_TERM_BORROWINGS = "SHORT_TERM_BORROWINGS", "Short-term borrowings"
    # Income statement.
    REVENUE = "REVENUE", "Revenue"
    COST_OF_SALES = "COST_OF_SALES", "Cost of sales"
    OTHER_INCOME = "OTHER_INCOME", "Other income"
    DISTRIBUTION_COSTS = "DISTRIBUTION_COSTS", "Distribution costs"
    ADMIN_EXPENSES = "ADMIN_EXPENSES", "Administrative expenses"
    OTHER_EXPENSES = "OTHER_EXPENSES", "Other expenses"
    FINANCE_COSTS = "FINANCE_COSTS", "Finance costs"
    TAX_EXPENSE = "TAX_EXPENSE", "Income tax expense"


#: Fallback IFRS-for-SMEs line for an account whose ``ifrs_line`` is unset, derived
#: from its :class:`AccountType` so a customised chart still presents coherently.
DEFAULT_IFRS_LINE_BY_TYPE = {
    AccountType.ASSET: IFRSLine.OTHER_CURRENT_ASSETS,
    AccountType.LIABILITY: IFRSLine.TRADE_PAYABLES,
    AccountType.EQUITY: IFRSLine.OTHER_RESERVES,
    AccountType.INCOME: IFRSLine.OTHER_INCOME,
    AccountType.EXPENSE: IFRSLine.OTHER_EXPENSES,
}


#: Well-known Chart-of-Accounts codes the Phase-4 services resolve by code. Kept here
#: (not hard-coded in services) so an entity with a customised chart can be remapped in
#: one place. All are seeded by :mod:`vs_finance.seed`.
PPE_ACCOUNT_CODE = "1500"                 # Property, Plant & Equipment (asset)
ACCUM_DEPRECIATION_CODE = "1900"          # Accumulated depreciation (contra-asset)
ACCRUED_REIMBURSEMENT_CODE = "2400"       # Staff expense-claim liability
PETTY_CASH_CODE = "1110"                  # Petty cash float (asset, child of 1100)
OUTPUT_VAT_CODE = "2200"                  # Output VAT payable (liability) - sales collect here
INPUT_VAT_CODE = "1300"                   # Input VAT recoverable (asset) - purchases offset here
WHT_PAYABLE_CODE = "2300"                 # Withholding-tax payable (liability)
PAYE_PAYABLE_CODE = "2310"                # PAYE (employee income tax) payable
PENSION_PAYABLE_CODE = "2320"             # Pension payable
NET_WAGES_PAYABLE_CODE = "2330"           # Net wages payable (cleared on disbursement)
SALARIES_EXPENSE_CODE = "5200"            # Salaries & wages expense
NHF_PAYABLE_CODE = "2340"                 # National Housing Fund payable
NSITF_PAYABLE_CODE = "2350"               # NSITF employee compensation payable
ITF_PAYABLE_CODE = "2360"                 # ITF training levy payable
EMPLOYER_PENSION_EXPENSE_CODE = "5210"    # Employer pension contributions expense
NSITF_EXPENSE_CODE = "5220"               # NSITF employee compensation expense
ITF_EXPENSE_CODE = "5230"                 # ITF training levy expense
DEPRECIATION_EXPENSE_CODE = "5400"        # Depreciation expense
BANK_CHARGES_CODE = "5500"               # Bank charges expense
RETAINED_EARNINGS_CODE = "3200"          # Retained earnings (equity) - net income closes here
OPERATING_REVENUE_CODE = "4100"          # Operating revenue (income) - generic revenue line
CASH_BANK_CODE = "1100"                  # Cash & bank (the cash-flow statement's cash line)
SALES_RETURNS_CODE = "4900"              # Sales returns (contra-revenue) - credit notes default here
DISCOUNTS_ALLOWED_CODE = "4910"          # Discounts & allowances (contra-revenue) - concessions default here
BAD_DEBT_EXPENSE_CODE = "5350"           # Bad debts (expense) - write-offs and provisions default here
CUSTOMER_CREDIT_CODE = "2140"            # Customer credit balances (liability) - overpayments / unapplied credit / refundable


#: The accounts a school's two readers name differently, in the words each is given.
#: A bursar's screens, refusals and messages say what the account holds in plain
#: words (``*_PLAIN``); an accountant's screens (the chart of accounts, the account
#: mapping, journals and posting previews, the close checklist, the reconciliation
#: reports) give the accounting term with the plain words beside it (``*_NAME``),
#: so neither reader has to translate. Each pair is the one source for the seeded
#: account's name, its mapping role's label and every journal line or check that
#: names it, so a posted document and the preview of a new one always agree.
GRIR_PLAIN = "goods received, not yet billed"
GRIR_NAME = f"GR/IR clearing ({GRIR_PLAIN})"
AR_PLAIN = "what customers owe"
AR_NAME = f"Accounts receivable ({AR_PLAIN})"
AP_PLAIN = "what is owed to suppliers"
AP_NAME = f"Accounts payable ({AP_PLAIN})"
WHT_PLAIN = "withholding tax"
WHT_NAME = f"WHT payable ({WHT_PLAIN})"
GATEWAY_PLAIN = "online payments not yet in the bank"
GATEWAY_NAME = f"Gateway clearing ({GATEWAY_PLAIN})"


class AccountMappingKey(models.TextChoices):
    """Entity-level account roles used by Finance and Procurement services."""

    CASH_BANK = "CASH_BANK", "Cash and bank"
    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE", AR_NAME
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE", AP_NAME
    CUSTOMER_CREDIT = "CUSTOMER_CREDIT", "Customer credit"
    VENDOR_ADVANCE = "VENDOR_ADVANCE", "Vendor advances"
    GRIR_CLEARING = "GRIR_CLEARING", GRIR_NAME
    OUTPUT_VAT = "OUTPUT_VAT", "Output VAT"
    WHT_PAYABLE = "WHT_PAYABLE", WHT_NAME
    RETAINED_EARNINGS = "RETAINED_EARNINGS", "Retained earnings"
    BAD_DEBT_EXPENSE = "BAD_DEBT_EXPENSE", "Bad debt expense"
    BANK_CHARGES = "BANK_CHARGES", "Bank charges"
    INVENTORY_ASSET = "INVENTORY_ASSET", "Inventory asset"
    INVENTORY_ADJUSTMENT = "INVENTORY_ADJUSTMENT", "Inventory adjustment"
    PURCHASE_PRICE_VARIANCE = "PURCHASE_PRICE_VARIANCE", "Purchase price variance"
    DEFERRED_INCOME = "DEFERRED_INCOME", "Deferred income"
    DEPOSITS_HELD = "DEPOSITS_HELD", "Customer deposits held"
    DOUBTFUL_DEBT_ALLOWANCE = "DOUBTFUL_DEBT_ALLOWANCE", "Allowance for doubtful debts"
    BAD_DEBT_RECOVERED = "BAD_DEBT_RECOVERED", "Bad debts recovered"
    FORFEITED_DEPOSIT_INCOME = "FORFEITED_DEPOSIT_INCOME", "Forfeited deposits income"
    GATEWAY_CLEARING = "GATEWAY_CLEARING", GATEWAY_NAME
    # Platform books only: what the platform's provider balance holds for each
    # client branch whose online money it keeps, and the balance itself.
    CLIENT_FUNDS_HELD = "CLIENT_FUNDS_HELD", "Client funds held"
    PROVIDER_BALANCE = "PROVIDER_BALANCE", "Payment provider balance"
    CLIENT_FUNDS_OWED = "CLIENT_FUNDS_OWED", "Owed by clients"
    # Online payments a payer's bank took back (a chargeback) on held money.
    CHARGEBACKS = "CHARGEBACKS", "Payment chargebacks"
    # What one branch is owed by, or owes to, each other branch. Every line on it
    # names its counterparty branch, and across every branch it nets to zero.
    INTER_BRANCH = "INTER_BRANCH", "Inter-branch balances"
    # Money one branch received that belongs to another, until it is forwarded.
    HELD_FOR_OTHER_BRANCHES = "HELD_FOR_OTHER_BRANCHES", "Held for other branches"
    # What a petty cash count finds over or short against the fund's books.
    CASH_OVER_SHORT = "CASH_OVER_SHORT", "Cash over and short"

#: Reserved code for CodeX's own platform set of books (the operator's entity).
#: An uppercase identifier (like all entity codes); the display name is "CodeX".
PLATFORM_ENTITY_CODE = "CODEX"

# --------------------------------------------------------------------------- #
# Adjustment-approval defaults (see vs_finance.approvals)                      #
# --------------------------------------------------------------------------- #

#: Template code for the seeded finance ladders. One per document type, matching
#: procurement and payouts so a tenant holds one mental model for approval.
WF_DEFAULT_TEMPLATE_CODE = "standard"

#: Codes of the approver groups a tenant's seeded adjustment and expense-claim stages
#: name. Each group is created empty by the seed, so a ladder arrives blocked rather
#: than open: the first document parks until the tenant puts somebody in the group.
WF_ADJUSTMENT_APPROVER_GROUP = "finance-adjustment-approver"
WF_SENIOR_ADJUSTMENT_APPROVER_GROUP = "finance-senior-adjustment-approver"
WF_EXPENSE_CLAIM_APPROVER_GROUP = "finance-expense-claim-approver"

#: Kobo at or above which a concession or credit note needs a second person.
#:
#: ₦50,000. Deliberately far below procurement's ₦500,000 senior bar, because these
#: are different risks. A purchase at ₦400,000 still buys the entity something; a
#: waiver at ₦400,000 is income given away, and a term's fees can sit well under
#: procurement's bar. Small goodwill allowances stay frictionless, which is the only
#: reason not to gate everything.
#:
#: Overridable per call to ``ensure_tenant_approval_templates`` and on the seed
#: command, so a tenant that wants every waiver approved sets it to zero.
WF_ADJUSTMENT_THRESHOLD = 5_000_000


# --------------------------------------------------------------------------- #
# One payer paying for several customers (see vs_finance.payer_payments)       #
# --------------------------------------------------------------------------- #

class PayerPaymentSplit(models.TextChoices):
    """How a payer's payment is shared among the customers it pays for.

    Each is a proposal the bursar may always override with explicit amounts per
    customer. ``OLDEST_FIRST`` walks every open bill of every customer the payer
    pays for, oldest first, as one list. ``PROPORTIONAL`` gives each customer a
    share in proportion to what they owe, oldest bill first within it.
    ``AS_ENTERED`` proposes nothing: the bursar types each customer's amount.
    """
    OLDEST_FIRST = "OLDEST_FIRST", "Oldest bill first, across every customer"
    PROPORTIONAL = "PROPORTIONAL", "In proportion to what each customer owes"
    AS_ENTERED = "AS_ENTERED", "As the bursar enters it for each customer"


class PayerPaymentSurplus(models.TextChoices):
    """Whose credit the part of a payer's payment no bill takes becomes.

    ``MOST_RECENT_BILL`` leaves it with the customer whose bill is newest, the one
    most likely to be billed again, so the credit pays their next bill on its own
    where the books apply credit automatically. ``PAYER`` leaves it on the payer's
    own account, for a payer who wants to decide later where it goes.
    """
    MOST_RECENT_BILL = "MOST_RECENT_BILL", "The customer with the most recent bill"
    PAYER = "PAYER", "The payer's own account"


#: The approver group the ready-made petty cash return route names. Created empty
#: when a tenant adopts the route, so the first return it stops parks until somebody
#: is put in the group.
WF_PETTY_CASH_RETURN_APPROVER_GROUP = "finance-petty-cash-approver"

#: Kobo above which a petty cash count's shortage needs a second person, on the
#: ready-made route a tenant may adopt. ₦5,000: small change lost from a busy tin
#: posts on the custodian's word, a missing ₦26,000 does not. A tenant chooses its
#: own figure when it adopts the route, and edits it like any other step afterwards.
WF_PETTY_CASH_SHORTAGE_THRESHOLD = 500_000


class PayBroughtForwardSource(models.TextChoices):
    """Whose pay a person's figures brought forward into a tax year are.

    * ``PREVIOUS_EMPLOYER``: another employer's, before the person joined. They
      count in the person's cumulative PAYE and are reported by that employer,
      never in this one's returns or year to date.
    * ``THIS_EMPLOYER``: this employer's own, for the months of the year before
      its payroll ran on these books. They count in cumulative PAYE and in this
      employer's year to date and annual return, and in no monthly remittance
      schedule, because those months were remitted from wherever payroll ran
      then.
    """

    PREVIOUS_EMPLOYER = "PREVIOUS_EMPLOYER", "Previous employer"
    THIS_EMPLOYER = "THIS_EMPLOYER", "This employer, before payroll ran here"


class BankSplitDifferenceTreatment(models.TextChoices):
    """What a shared bank split does with a branch's book balance that differs from its share.

    Ikeja and Lekki share a GTBank account holding N400,000. Ikeja's entries on
    it total N500,000 and Lekki's minus N100,000, and the bursars agree Ikeja
    takes N250,000 and Lekki N150,000.

    * ``DEBT``: Lekki now holds N250,000 of Ikeja's cash, so Lekki owes Ikeja
      N250,000 on the inter-branch balances and settles it later with a cash
      transfer. Neither branch's retained earnings move.
    * ``PERMANENT_MOVE``: the N250,000 passes through retained earnings, so
      Ikeja's equity falls and Lekki's rises by that much, and nobody owes
      anybody.

    Chosen per split. A split where every share equals its book balance has no
    difference, and posts the same journals under either choice.
    """

    DEBT = "DEBT", "Debt between branches"
    PERMANENT_MOVE = "PERMANENT_MOVE", "Permanent move through retained earnings"
