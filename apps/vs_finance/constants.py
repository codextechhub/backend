"""Shared enumerations and constants for the finance engine.

Defined once here so Phase-1 models (Account, FiscalPeriod, JournalEntry …) and the
posting service agree on the same vocabulary without circular imports.
"""
from __future__ import annotations  # Import dependency used by this finance module.

from django.db import models  # Import dependency used by this finance module.


class PeriodStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a fiscal period.

    OPEN          -> postings allowed.
    SOFT_CLOSED   -> normal users blocked; admins/auto-postings (e.g. depreciation
                     at close) may still post. Reversible.
    CLOSED        -> no postings; reversible only by an explicit re-open with audit.
    LOCKED        -> permanently sealed (e.g. after statutory filing). No postings.
    """
    OPEN = "OPEN", "Open"  # Store intermediate finance value.
    SOFT_CLOSED = "SOFT_CLOSED", "Soft Closed"  # Store intermediate finance value.
    CLOSED = "CLOSED", "Closed"  # Store intermediate finance value.
    LOCKED = "LOCKED", "Locked"  # Store intermediate finance value.


#: Statuses into which an ordinary journal may NOT be posted.
PERIOD_POSTING_BLOCKED = frozenset({  # Store intermediate finance value.
    PeriodStatus.CLOSED,  # Finance processing step.
    PeriodStatus.LOCKED,  # Finance processing step.
})  # Continue structured finance payload.

#: Statuses into which only privileged/system postings (close auto-entries) may go.
PERIOD_POSTING_RESTRICTED = frozenset({  # Store intermediate finance value.
    PeriodStatus.SOFT_CLOSED,  # Finance processing step.
})  # Continue structured finance payload.


class DocumentStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Generic lifecycle for numbered finance documents.

    Concrete documents (invoices, POs, journals) may use a subset or extend this;
    the abstract :class:`~vs_finance.models.FinanceDocument` defaults to it.
    """
    DRAFT = "DRAFT", "Draft"  # Store intermediate finance value.
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"  # Store intermediate finance value.
    APPROVED = "APPROVED", "Approved"  # Store intermediate finance value.
    POSTED = "POSTED", "Posted"  # Store intermediate finance value.
    REVERSED = "REVERSED", "Reversed"  # Store intermediate finance value.
    CANCELLED = "CANCELLED", "Cancelled"  # Store intermediate finance value.


class DocType(models.TextChoices):  # Class groups related finance API or service behavior.
    """Document-type tokens used by the numbering sequence.

    The token becomes the middle segment of a document number, e.g. ``INV`` in
    ``CFX-B01-INV-2026-00821``. Keep tokens short, uppercase and stable — they are
    persisted inside human-facing identifiers.
    """
    JOURNAL = "JNL", "Journal Entry"  # Store intermediate finance value.
    INVOICE = "INV", "Sales / AR Invoice"  # Store intermediate finance value.
    RECEIPT = "RCP", "Receipt"  # Store intermediate finance value.
    PAYMENT = "PAY", "Payment"  # Store intermediate finance value.
    CREDIT_NOTE = "CRN", "Credit Note"  # Store intermediate finance value.
    DEBIT_NOTE = "DRN", "Debit Note"  # Store intermediate finance value.
    REFUND = "RFD", "Customer Refund"  # Store intermediate finance value.
    PAYMENT_PLAN = "PPL", "Installment Payment Plan"  # Store intermediate finance value.
    CONCESSION = "CNC", "Concession / Discount / Waiver"  # Store intermediate finance value.
    WRITE_OFF = "WOF", "Bad-debt Write-off"  # Store intermediate finance value.
    DUNNING_NOTICE = "DUN", "Dunning / Payment Reminder"  # Store intermediate finance value.
    PURCHASE_REQUISITION = "PR", "Purchase Requisition"  # Store intermediate finance value.
    RFQ = "RFQ", "Request for Quotation"  # Store intermediate finance value.
    QUOTATION = "QUO", "Vendor Quotation"  # Store intermediate finance value.
    PURCHASE_ORDER = "PO", "Purchase Order"  # Store intermediate finance value.
    GOODS_RECEIVED = "GRN", "Goods Received Note"  # Store intermediate finance value.
    VENDOR_INVOICE = "VIN", "Vendor Invoice"  # Store intermediate finance value.
    VENDOR_PAYMENT = "VPY", "Vendor Payment"  # Store intermediate finance value.
    EXPENSE_CLAIM = "EXP", "Expense Claim"  # Store intermediate finance value.
    PETTY_CASH_VOUCHER = "PCV", "Petty Cash Voucher"  # Store intermediate finance value.
    PAYROLL_RUN = "PYR", "Payroll Run"  # Store intermediate finance value.
    FIXED_ASSET = "FA", "Fixed Asset"  # Store intermediate finance value.
    TAX_FILING = "TXF", "Tax Filing / Remittance"  # Store intermediate finance value.
    BUDGET = "BDG", "Budget"  # Store intermediate finance value.

class AccountType(models.TextChoices):  # Class groups related finance API or service behavior.
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
    ASSET = "ASSET", "Asset"  # Store intermediate finance value.
    LIABILITY = "LIABILITY", "Liability"  # Store intermediate finance value.
    EQUITY = "EQUITY", "Equity"  # Store intermediate finance value.
    INCOME = "INCOME", "Income"  # Store intermediate finance value.
    EXPENSE = "EXPENSE", "Expense"  # Store intermediate finance value.


class NormalBalance(models.TextChoices):  # Class groups related finance API or service behavior.
    """The side on which an account normally carries its balance."""
    DEBIT = "DEBIT", "Debit"  # Store intermediate finance value.
    CREDIT = "CREDIT", "Credit"  # Store intermediate finance value.


class FeeAppliesTo(models.TextChoices):  # Class groups related finance API or service behavior.
    """Who a :class:`vs_finance.models.FeeStructure` bills.

    This is a *generic* platform — a fee structure is not tied to a school term.
    It classifies the counterparty type the template charges: a client/customer
    (e.g. a school's students/payers), a vendor, a staff member, or a general
    template not bound to any counterparty type. Only ``CUSTOMER`` structures can
    currently generate AR invoices.
    """
    CUSTOMER = "CUSTOMER", "Customer"  # Store intermediate finance value.
    VENDOR = "VENDOR", "Vendor"  # Store intermediate finance value.
    STAFF = "STAFF", "Staff"  # Store intermediate finance value.
    GENERAL = "GENERAL", "General"  # Store intermediate finance value.


#: Default natural balance for each account root (before any contra flip).
NORMAL_BALANCE_BY_TYPE = {  # Store intermediate finance value.
    AccountType.ASSET: NormalBalance.DEBIT,  # Finance processing step.
    AccountType.EXPENSE: NormalBalance.DEBIT,  # Finance processing step.
    AccountType.LIABILITY: NormalBalance.CREDIT,  # Finance processing step.
    AccountType.EQUITY: NormalBalance.CREDIT,  # Finance processing step.
    AccountType.INCOME: NormalBalance.CREDIT,  # Finance processing step.
}  # Continue structured finance payload.


class JournalSource(models.TextChoices):  # Class groups related finance API or service behavior.
    """Where a journal entry originated — for filtering and audit, not for posting logic.

    MANUAL entries are typed by a person; the rest are raised by sub-ledgers and
    automated processes (AR/AP postings, bank reconciliation, period-close accruals
    and depreciation, opening balances, FX revaluation).
    """
    MANUAL = "MANUAL", "Manual"  # Store intermediate finance value.
    SALES = "SALES", "Sales / AR"  # Store intermediate finance value.
    PURCHASE = "PURCHASE", "Purchase / AP"  # Store intermediate finance value.
    BANK = "BANK", "Bank / Cash"  # Store intermediate finance value.
    PAYROLL = "PAYROLL", "Payroll"  # Store intermediate finance value.
    CLOSING = "CLOSING", "Period Close"  # Store intermediate finance value.
    OPENING = "OPENING", "Opening Balance"  # Store intermediate finance value.
    FX = "FX", "FX Revaluation"  # Store intermediate finance value.
    SYSTEM = "SYSTEM", "System"  # Store intermediate finance value.


class InvoiceSource(models.TextChoices):  # Class groups related finance API or service behavior.
    """What generated an invoice — keeps the AR core domain-neutral.

    The invoice model is generic; ``source`` records the originating mechanism so a
    school-fee run, a subscription engine or an API caller can all emit the *same*
    generic :class:`~vs_finance.models.Invoice` without the ledger knowing about any
    of them. Student/fee concepts live only in the adapter that sets ``FEE_BILLING``.
    """
    MANUAL = "MANUAL", "Manual"  # Store intermediate finance value.
    FEE_BILLING = "FEE_BILLING", "Fee Billing"  # Store intermediate finance value.
    SUBSCRIPTION = "SUBSCRIPTION", "Subscription"  # Store intermediate finance value.
    API = "API", "API"  # Store intermediate finance value.
    OPENING = "OPENING", "Opening Balance"  # Store intermediate finance value.


class InvoicePaymentStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """How much of an invoice has been settled — distinct from its document status.

    Document ``status`` (DRAFT→POSTED→…) tracks the *ledger* lifecycle; this tracks
    *cash* against the invoice and is derived from amount paid vs total.
    """
    UNPAID = "UNPAID", "Unpaid"  # Store intermediate finance value.
    PARTIAL = "PARTIAL", "Partially Paid"  # Store intermediate finance value.
    PAID = "PAID", "Paid"  # Store intermediate finance value.


class CreditNoteKind(models.TextChoices):  # Class groups related finance API or service behavior.
    """Direction of a credit/debit note against a customer's receivable.

    CREDIT reduces what the customer owes (a sales return, allowance or correction:
    ``Dr revenue/returns + Dr output tax, Cr AR``); it may be *applied* to specific
    invoices like a non-cash payment. DEBIT increases what the customer owes (an extra
    charge or under-bill correction: ``Dr AR, Cr revenue + Cr output tax``) — a
    supplementary invoice, so it is never allocated to reduce another invoice.
    """
    CREDIT = "CREDIT", "Credit note (reduces AR)"  # Store intermediate finance value.
    DEBIT = "DEBIT", "Debit note (increases AR)"  # Store intermediate finance value.


class PaymentPlanFrequency(models.TextChoices):  # Class groups related finance API or service behavior.
    """Spacing between installments in a payment plan (drives each due date)."""
    WEEKLY = "WEEKLY", "Weekly"  # Store intermediate finance value.
    FORTNIGHTLY = "FORTNIGHTLY", "Fortnightly (every 2 weeks)"  # Store intermediate finance value.
    MONTHLY = "MONTHLY", "Monthly"  # Store intermediate finance value.
    QUARTERLY = "QUARTERLY", "Quarterly"  # Store intermediate finance value.


class PaymentPlanStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of an installment payment plan (a scheduling overlay, never posted).

    DRAFT      -> schedule being built; editable.
    ACTIVE     -> committed; installments are live and tracked against settlement.
    COMPLETED  -> every installment fully settled.
    CANCELLED  -> abandoned; no longer tracked.
    """
    DRAFT = "DRAFT", "Draft"  # Store intermediate finance value.
    ACTIVE = "ACTIVE", "Active"  # Store intermediate finance value.
    COMPLETED = "COMPLETED", "Completed"  # Store intermediate finance value.
    CANCELLED = "CANCELLED", "Cancelled"  # Store intermediate finance value.


class InstallmentStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Settlement state of a single installment, derived from amount settled vs due."""
    PENDING = "PENDING", "Pending"  # Store intermediate finance value.
    PARTIAL = "PARTIAL", "Partially settled"  # Store intermediate finance value.
    PAID = "PAID", "Settled"  # Store intermediate finance value.


class ConcessionKind(models.TextChoices):  # Class groups related finance API or service behavior.
    """A non-cash reduction of a receivable granted to a customer.

    DISCOUNT    -> commercial/early-settlement price reduction.
    WAIVER      -> charge forgiven (e.g. a penalty or fee dropped).
    SCHOLARSHIP -> a granted allowance against billed amounts (domain-neutral name for
                   a bursary/scholarship in a school tenant).

    All three post the same way — ``Dr discounts & allowances, Cr AR control`` — and
    reduce the invoice's balance via :attr:`Invoice.amount_credited`; ``kind`` is a
    reporting tag, not a different posting.
    """
    DISCOUNT = "DISCOUNT", "Discount"  # Store intermediate finance value.
    WAIVER = "WAIVER", "Waiver"  # Store intermediate finance value.
    SCHOLARSHIP = "SCHOLARSHIP", "Scholarship / bursary"  # Store intermediate finance value.


class DunningChannel(models.TextChoices):  # Class groups related finance API or service behavior.
    """How a dunning reminder is delivered (operational detail; vs_finance only records it).

    vs_finance does not itself send email/SMS — it tracks the *intent* and outcome; an
    outer service (notifications) reads PENDING notices and dispatches them.
    """
    EMAIL = "EMAIL", "Email"  # Store intermediate finance value.
    IN_APP = "IN_APP", "In-app"  # Store intermediate finance value.


class DunningNoticeStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a single dunning notice (a communications overlay, never posted).

    PENDING   -> generated, awaiting dispatch.
    SENT      -> dispatched to the customer.
    RESOLVED  -> the underlying invoice was settled (or written off) after the notice.
    CANCELLED -> withdrawn before sending (e.g. a payment arrived, or a dispute opened).
    """
    PENDING = "PENDING", "Pending"  # Store intermediate finance value.
    SENT = "SENT", "Sent"  # Store intermediate finance value.
    RESOLVED = "RESOLVED", "Resolved"  # Store intermediate finance value.
    CANCELLED = "CANCELLED", "Cancelled"  # Store intermediate finance value.


class PaymentMethod(models.TextChoices):  # Class groups related finance API or service behavior.
    """How a customer receipt was tendered (operational detail, not posting logic)."""
    CASH = "CASH", "Cash"  # Store intermediate finance value.
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"  # Store intermediate finance value.
    CARD = "CARD", "Card"  # Store intermediate finance value.
    CHEQUE = "CHEQUE", "Cheque"  # Store intermediate finance value.
    ONLINE = "ONLINE", "Online / Gateway"  # Store intermediate finance value.
    OTHER = "OTHER", "Other"  # Store intermediate finance value.


# --------------------------------------------------------------------------- #
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close     #
# --------------------------------------------------------------------------- #

class BankLineStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Reconciliation state of an imported bank-statement line.

    UNMATCHED -> not yet paired with a ledger movement.
    MATCHED   -> reconciled to a GL bank-account journal line.
    IGNORED   -> intentionally excluded (duplicate, opening line, etc.).
    """
    UNMATCHED = "UNMATCHED", "Unmatched"  # Store intermediate finance value.
    MATCHED = "MATCHED", "Matched"  # Store intermediate finance value.
    IGNORED = "IGNORED", "Ignored"  # Store intermediate finance value.


class BankMatchSource(models.TextChoices):  # Class groups related finance API or service behavior.
    """How a statement line came to be matched."""
    AUTO = "AUTO", "Auto"  # Store intermediate finance value.
    MANUAL = "MANUAL", "Manual"  # Store intermediate finance value.
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"  # Store intermediate finance value.


class BankStatementStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of an imported bank statement (a batch of lines for a period)."""
    UPLOADED = "UPLOADED", "Uploaded"  # Store intermediate finance value.
    RECONCILED = "RECONCILED", "Reconciled"  # Store intermediate finance value.


class BankReconStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Outcome of a reconciliation run."""
    BALANCED = "BALANCED", "Balanced"  # Store intermediate finance value.
    OUT_OF_BALANCE = "OUT_OF_BALANCE", "Out of balance"  # Store intermediate finance value.


class PayrollRunStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a payroll run (a batch of employee pay lines)."""
    DRAFT = "DRAFT", "Draft"  # Store intermediate finance value.
    POSTED = "POSTED", "Posted (accrued)"  # Store intermediate finance value.
    PAID = "PAID", "Paid (disbursed)"  # Store intermediate finance value.
    CANCELLED = "CANCELLED", "Cancelled"  # Store intermediate finance value.


class SalaryComponentKind(models.TextChoices):  # Class groups related finance API or service behavior.
    """Whether a salary-structure component adds to pay or is withheld from it."""
    EARNING = "EARNING", "Earning"  # Store intermediate finance value.
    DEDUCTION = "DEDUCTION", "Deduction"  # Store intermediate finance value.


class SalaryCalcMethod(models.TextChoices):  # Class groups related finance API or service behavior.
    """How a salary component's amount is derived from an employee's gross."""
    FIXED = "FIXED", "Fixed amount"  # Store intermediate finance value.
    PERCENT_OF_GROSS = "PERCENT_OF_GROSS", "Percent of gross"  # Store intermediate finance value.
    PERCENT_OF_BASIC = "PERCENT_OF_BASIC", "Percent of basic"  # Store intermediate finance value.


class StatutoryType(models.TextChoices):  # Class groups related finance API or service behavior.
    """Which statutory liability a deduction feeds — routes the GL credit and the return.

    Earnings are always ``NONE``; deductions must be ``PAYE`` or ``PENSION`` so the
    accrual journal stays balanced (``net = gross - paye - pension``).
    """
    NONE = "NONE", "None"  # Store intermediate finance value.
    PAYE = "PAYE", "PAYE"  # Store intermediate finance value.
    PENSION = "PENSION", "Pension"  # Store intermediate finance value.


class BudgetStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a budget; approval locks the figures so actuals can't be re-planned.

    Two states only: a DRAFT budget is editable; APPROVED locks it (see
    :attr:`Budget.is_locked`). There is no separate LOCKED state — approval *is* the lock.
    """
    DRAFT = "DRAFT", "Draft"  # Store intermediate finance value.
    APPROVED = "APPROVED", "Approved"  # Store intermediate finance value.


class DepreciationMethod(models.TextChoices):  # Class groups related finance API or service behavior.
    """Depreciation method for a fixed asset."""
    STRAIGHT_LINE = "STRAIGHT_LINE", "Straight line"  # Store intermediate finance value.
    DECLINING_BALANCE = "DECLINING_BALANCE", "Declining balance"  # Store intermediate finance value.


class AssetStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a fixed asset in the register."""
    DRAFT = "DRAFT", "Draft"  # Store intermediate finance value.
    ACTIVE = "ACTIVE", "Active"  # Store intermediate finance value.
    FULLY_DEPRECIATED = "FULLY_DEPRECIATED", "Fully depreciated"  # Store intermediate finance value.
    DISPOSED = "DISPOSED", "Disposed"  # Store intermediate finance value.


class AssetCategory(models.TextChoices):  # Class groups related finance API or service behavior.
    """Broad register category for a fixed asset (drives the list filter/column)."""
    VEHICLES = "VEHICLES", "Vehicles"  # Store intermediate finance value.
    BUILDINGS = "BUILDINGS", "Buildings"  # Store intermediate finance value.
    PLANT_MACHINERY = "PLANT_MACHINERY", "Plant & machinery"  # Store intermediate finance value.
    IT_EQUIPMENT = "IT_EQUIPMENT", "IT equipment"  # Store intermediate finance value.
    FURNITURE = "FURNITURE", "Furniture & fittings"  # Store intermediate finance value.
    EQUIPMENT = "EQUIPMENT", "Equipment"  # Store intermediate finance value.
    OTHER = "OTHER", "Other"  # Store intermediate finance value.


class FinanceAuditAction(models.TextChoices):  # Class groups related finance API or service behavior.
    """Auditable finance actions recorded in the in-app, append-only audit log.

    The ledger itself (immutable posted/reversed journals + period locks) is the
    primary financial audit trail; this enum names the *actions around* it that the
    journals can't capture on their own — who pressed post, rejected attempts, period
    state changes and master-data edits.
    """
    JOURNAL_POSTED = "JOURNAL_POSTED", "Journal posted"  # Store intermediate finance value.
    JOURNAL_REVERSED = "JOURNAL_REVERSED", "Journal reversed"  # Store intermediate finance value.
    JOURNAL_POST_REJECTED = "JOURNAL_POST_REJECTED", "Journal posting rejected"  # Store intermediate finance value.
    INVOICE_POSTED = "INVOICE_POSTED", "Invoice posted"  # Store intermediate finance value.
    INVOICE_CANCELLED = "INVOICE_CANCELLED", "Invoice cancelled"  # Store intermediate finance value.
    INVOICE_WRITTEN_OFF = "INVOICE_WRITTEN_OFF", "Invoice written off (bad debt)"  # Store intermediate finance value.
    PAYMENT_POSTED = "PAYMENT_POSTED", "Payment posted"  # Store intermediate finance value.
    PAYMENT_ALLOCATED = "PAYMENT_ALLOCATED", "Payment allocated"  # Store intermediate finance value.
    CREDIT_NOTE_POSTED = "CREDIT_NOTE_POSTED", "Credit note posted"  # Store intermediate finance value.
    CREDIT_NOTE_ALLOCATED = "CREDIT_NOTE_ALLOCATED", "Credit note allocated"  # Store intermediate finance value.
    DEBIT_NOTE_POSTED = "DEBIT_NOTE_POSTED", "Debit note posted"  # Store intermediate finance value.
    REFUND_POSTED = "REFUND_POSTED", "Customer refund posted"  # Store intermediate finance value.
    PAYMENT_PLAN_ACTIVATED = "PAYMENT_PLAN_ACTIVATED", "Installment plan activated"  # Store intermediate finance value.
    PAYMENT_PLAN_COMPLETED = "PAYMENT_PLAN_COMPLETED", "Installment plan completed"  # Store intermediate finance value.
    PAYMENT_PLAN_CANCELLED = "PAYMENT_PLAN_CANCELLED", "Installment plan cancelled"  # Store intermediate finance value.
    CONCESSION_POSTED = "CONCESSION_POSTED", "Concession / discount / waiver posted"  # Store intermediate finance value.
    DUNNING_RUN_GENERATED = "DUNNING_RUN_GENERATED", "Dunning run generated"  # Store intermediate finance value.
    DUNNING_NOTICE_SENT = "DUNNING_NOTICE_SENT", "Dunning notice marked sent"  # Store intermediate finance value.
    DUNNING_NOTICE_CANCELLED = "DUNNING_NOTICE_CANCELLED", "Dunning notice cancelled"  # Store intermediate finance value.
    PERIOD_CLOSED = "PERIOD_CLOSED", "Period closed"  # Store intermediate finance value.
    PERIOD_REOPENED = "PERIOD_REOPENED", "Period re-opened"  # Store intermediate finance value.
    ACCOUNT_CREATED = "ACCOUNT_CREATED", "Account created"  # Store intermediate finance value.
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED", "Account updated"  # Store intermediate finance value.
    # Procure-to-Pay. The vendor/PO/GRN documents live in vs_procurement,
    # but their audit vocabulary belongs to finance's authoritative log (finance does
    # not import procurement — these are just string constants).
    REQUISITION_APPROVED = "REQUISITION_APPROVED", "Requisition approved"  # Store intermediate finance value.
    RFQ_ISSUED = "RFQ_ISSUED", "Request for quotation issued"  # Store intermediate finance value.
    RFQ_CANCELLED = "RFQ_CANCELLED", "Request for quotation cancelled"  # Store intermediate finance value.
    QUOTATION_SUBMITTED = "QUOTATION_SUBMITTED", "Vendor quotation submitted"  # Store intermediate finance value.
    QUOTATION_AWARDED = "QUOTATION_AWARDED", "Vendor quotation awarded → PO"  # Store intermediate finance value.
    VENDOR_CONTRACT_ACTIVATED = "VENDOR_CONTRACT_ACTIVATED", "Vendor contract activated"  # Store intermediate finance value.
    VENDOR_CONTRACT_RENEWED = "VENDOR_CONTRACT_RENEWED", "Vendor contract renewed"  # Store intermediate finance value.
    VENDOR_CONTRACT_TERMINATED = "VENDOR_CONTRACT_TERMINATED", "Vendor contract terminated"  # Store intermediate finance value.
    CONTRACT_MILESTONE_COMPLETED = "CONTRACT_MILESTONE_COMPLETED", "Contract milestone completed"  # Store intermediate finance value.
    PURCHASE_ORDER_APPROVED = "PURCHASE_ORDER_APPROVED", "Purchase order approved"  # Store intermediate finance value.
    GRN_POSTED = "GRN_POSTED", "Goods receipt posted"  # Store intermediate finance value.
    GRN_POST_REJECTED = "GRN_POST_REJECTED", "Goods receipt posting rejected"  # Store intermediate finance value.
    VENDOR_INVOICE_MATCHED = "VENDOR_INVOICE_MATCHED", "Vendor invoice matched"  # Store intermediate finance value.
    VENDOR_INVOICE_APPROVED = "VENDOR_INVOICE_APPROVED", "Vendor invoice approved (workflow)"  # Store intermediate finance value.
    VENDOR_INVOICE_POSTED = "VENDOR_INVOICE_POSTED", "Vendor invoice posted"  # Store intermediate finance value.
    VENDOR_INVOICE_POST_REJECTED = "VENDOR_INVOICE_POST_REJECTED", "Vendor invoice posting rejected"  # Store intermediate finance value.
    VENDOR_PAYMENT_POSTED = "VENDOR_PAYMENT_POSTED", "Vendor payment posted"  # Store intermediate finance value.
    VENDOR_PAYMENT_POST_REJECTED = "VENDOR_PAYMENT_POST_REJECTED", "Vendor payment posting rejected"  # Store intermediate finance value.
    VENDOR_PAYMENT_ALLOCATED = "VENDOR_PAYMENT_ALLOCATED", "Vendor payment allocated"  # Store intermediate finance value.
    STOCK_RECEIVED = "STOCK_RECEIVED", "Stock received (perpetual inventory)"  # Store intermediate finance value.
    STOCK_ISSUED = "STOCK_ISSUED", "Stock issued"  # Store intermediate finance value.
    STOCK_ISSUE_REJECTED = "STOCK_ISSUE_REJECTED", "Stock issue rejected"  # Store intermediate finance value.
    STOCK_ADJUSTED = "STOCK_ADJUSTED", "Stock adjusted"  # Store intermediate finance value.
    STOCK_ADJUST_REJECTED = "STOCK_ADJUST_REJECTED", "Stock adjustment rejected"  # Store intermediate finance value.
    # Phase 4 — banking, expenses, payroll, budget, fixed assets, period close.
    BANK_RECONCILED = "BANK_RECONCILED", "Bank statement reconciled"  # Store intermediate finance value.
    BANK_CHARGE_POSTED = "BANK_CHARGE_POSTED", "Bank charge posted"  # Store intermediate finance value.
    EXPENSE_CLAIM_POSTED = "EXPENSE_CLAIM_POSTED", "Expense claim posted"  # Store intermediate finance value.
    EXPENSE_CLAIM_POST_REJECTED = "EXPENSE_CLAIM_POST_REJECTED", "Expense claim posting rejected"  # Store intermediate finance value.
    EXPENSE_CLAIM_SETTLED = "EXPENSE_CLAIM_SETTLED", "Expense claim settled"  # Store intermediate finance value.
    EXPENSE_CLAIM_VOIDED = "EXPENSE_CLAIM_VOIDED", "Expense claim voided"  # Store intermediate finance value.
    PETTY_CASH_ESTABLISHED = "PETTY_CASH_ESTABLISHED", "Petty cash fund established / topped up"  # Store intermediate finance value.
    PETTY_CASH_VOUCHER_POSTED = "PETTY_CASH_VOUCHER_POSTED", "Petty cash voucher posted"  # Store intermediate finance value.
    PETTY_CASH_VOUCHER_REJECTED = "PETTY_CASH_VOUCHER_REJECTED", "Petty cash voucher rejected"  # Store intermediate finance value.
    PETTY_CASH_VOUCHER_VOIDED = "PETTY_CASH_VOUCHER_VOIDED", "Petty cash voucher voided"  # Store intermediate finance value.
    PETTY_CASH_REPLENISHED = "PETTY_CASH_REPLENISHED", "Petty cash fund replenished"  # Store intermediate finance value.
    PAYROLL_POSTED = "PAYROLL_POSTED", "Payroll run posted"  # Store intermediate finance value.
    PAYROLL_POST_REJECTED = "PAYROLL_POST_REJECTED", "Payroll run posting rejected"  # Store intermediate finance value.
    PAYROLL_PAID = "PAYROLL_PAID", "Payroll run disbursed"  # Store intermediate finance value.
    PAYROLL_CANCELLED = "PAYROLL_CANCELLED", "Payroll run cancelled / voided"  # Store intermediate finance value.
    BUDGET_APPROVED = "BUDGET_APPROVED", "Budget approved"  # Store intermediate finance value.
    BUDGET_DELETED = "BUDGET_DELETED", "Budget deleted"  # Store intermediate finance value.
    ASSET_ACQUIRED = "ASSET_ACQUIRED", "Fixed asset acquired"  # Store intermediate finance value.
    DEPRECIATION_POSTED = "DEPRECIATION_POSTED", "Depreciation posted"  # Store intermediate finance value.
    ASSET_DISPOSED = "ASSET_DISPOSED", "Fixed asset disposed"  # Store intermediate finance value.
    PERIOD_LOCKED = "PERIOD_LOCKED", "Period locked"  # Store intermediate finance value.
    TAX_FILING_PREPARED = "TAX_FILING_PREPARED", "Tax filing prepared"  # Store intermediate finance value.
    TAX_FILING_FILED = "TAX_FILING_FILED", "Tax filing submitted to authority"  # Store intermediate finance value.
    TAX_FILING_UNFILED = "TAX_FILING_UNFILED", "Tax filing un-filed (reverted to draft)"  # Store intermediate finance value.
    TAX_FILING_PAID = "TAX_FILING_PAID", "Tax filing paid / remitted"  # Store intermediate finance value.
    TAX_FILING_REJECTED = "TAX_FILING_REJECTED", "Tax filing action rejected"  # Store intermediate finance value.


class FinanceAuditStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Outcome of an audited action."""
    SUCCESS = "SUCCESS", "Success"  # Store intermediate finance value.
    FAILED = "FAILED", "Failed"  # Store intermediate finance value.


class TaxObligationType(models.TextChoices):  # Class groups related finance API or service behavior.
    """The statutory tax a remittance obligation covers."""
    VAT = "VAT", "Value Added Tax"  # Store intermediate finance value.
    WHT = "WHT", "Withholding Tax"  # Store intermediate finance value.
    PAYE = "PAYE", "Pay-As-You-Earn (employee income tax)"  # Store intermediate finance value.
    PENSION = "PENSION", "Pension contribution"  # Store intermediate finance value.
    OTHER = "OTHER", "Other statutory levy"  # Store intermediate finance value.


class TaxFilingFrequency(models.TextChoices):  # Class groups related finance API or service behavior.
    """How often a return falls due for an obligation."""
    MONTHLY = "MONTHLY", "Monthly"  # Store intermediate finance value.
    QUARTERLY = "QUARTERLY", "Quarterly"  # Store intermediate finance value.
    ANNUAL = "ANNUAL", "Annual"  # Store intermediate finance value.


class TaxFilingStatus(models.TextChoices):  # Class groups related finance API or service behavior.
    """Lifecycle of a single tax return: prepared, filed with the authority, paid."""
    DRAFT = "DRAFT", "Draft / prepared"  # Store intermediate finance value.
    FILED = "FILED", "Filed with authority"  # Store intermediate finance value.
    PAID = "PAID", "Paid / remitted"  # Store intermediate finance value.
    CANCELLED = "CANCELLED", "Cancelled"  # Store intermediate finance value.


class IFRSLine(models.TextChoices):  # Class groups related finance API or service behavior.
    """IFRS-for-SMEs presentation lines a chart account rolls up to.

    The five double-entry roots (:class:`AccountType`) decide *where* an account
    lands on the statements; this finer classification decides *which statutory line*
    it presents on, so the Statement of Financial Position and Income Statement read
    the way FIRS / CAC filings expect rather than as a raw account list. An account
    with a blank ``ifrs_line`` falls back to a type-derived default (see
    :data:`DEFAULT_IFRS_LINE_BY_TYPE`), so the mapping degrades gracefully on a
    customised chart.
    """
    # Statement of Financial Position — non-current assets.
    PPE = "PPE", "Property, plant and equipment"  # Store intermediate finance value.
    INTANGIBLES = "INTANGIBLES", "Intangible assets"  # Store intermediate finance value.
    INVESTMENTS = "INVESTMENTS", "Investments"  # Store intermediate finance value.
    # Statement of Financial Position — current assets.
    INVENTORIES = "INVENTORIES", "Inventories"  # Store intermediate finance value.
    TRADE_RECEIVABLES = "TRADE_RECEIVABLES", "Trade and other receivables"  # Store intermediate finance value.
    CURRENT_TAX_ASSET = "CURRENT_TAX_ASSET", "Current tax assets"  # Store intermediate finance value.
    CASH = "CASH", "Cash and cash equivalents"  # Store intermediate finance value.
    OTHER_CURRENT_ASSETS = "OTHER_CURRENT_ASSETS", "Other current assets"  # Store intermediate finance value.
    # Statement of Financial Position — equity.
    SHARE_CAPITAL = "SHARE_CAPITAL", "Share capital"  # Store intermediate finance value.
    RETAINED_EARNINGS = "RETAINED_EARNINGS", "Retained earnings"  # Store intermediate finance value.
    OTHER_RESERVES = "OTHER_RESERVES", "Other reserves"  # Store intermediate finance value.
    # Statement of Financial Position — non-current liabilities.
    LONG_TERM_BORROWINGS = "LONG_TERM_BORROWINGS", "Long-term borrowings"  # Store intermediate finance value.
    # Statement of Financial Position — current liabilities.
    TRADE_PAYABLES = "TRADE_PAYABLES", "Trade and other payables"  # Store intermediate finance value.
    CURRENT_TAX_PAYABLE = "CURRENT_TAX_PAYABLE", "Current tax payable"  # Store intermediate finance value.
    EMPLOYEE_PAYABLES = "EMPLOYEE_PAYABLES", "Employee benefit obligations"  # Store intermediate finance value.
    SHORT_TERM_BORROWINGS = "SHORT_TERM_BORROWINGS", "Short-term borrowings"  # Store intermediate finance value.
    # Income statement.
    REVENUE = "REVENUE", "Revenue"  # Store intermediate finance value.
    COST_OF_SALES = "COST_OF_SALES", "Cost of sales"  # Store intermediate finance value.
    OTHER_INCOME = "OTHER_INCOME", "Other income"  # Store intermediate finance value.
    DISTRIBUTION_COSTS = "DISTRIBUTION_COSTS", "Distribution costs"  # Store intermediate finance value.
    ADMIN_EXPENSES = "ADMIN_EXPENSES", "Administrative expenses"  # Store intermediate finance value.
    OTHER_EXPENSES = "OTHER_EXPENSES", "Other expenses"  # Store intermediate finance value.
    FINANCE_COSTS = "FINANCE_COSTS", "Finance costs"  # Store intermediate finance value.
    TAX_EXPENSE = "TAX_EXPENSE", "Income tax expense"  # Store intermediate finance value.


#: Fallback IFRS-for-SMEs line for an account whose ``ifrs_line`` is unset, derived
#: from its :class:`AccountType` so a customised chart still presents coherently.
DEFAULT_IFRS_LINE_BY_TYPE = {  # Store intermediate finance value.
    AccountType.ASSET: IFRSLine.OTHER_CURRENT_ASSETS,  # Finance processing step.
    AccountType.LIABILITY: IFRSLine.TRADE_PAYABLES,  # Finance processing step.
    AccountType.EQUITY: IFRSLine.OTHER_RESERVES,  # Finance processing step.
    AccountType.INCOME: IFRSLine.OTHER_INCOME,  # Finance processing step.
    AccountType.EXPENSE: IFRSLine.OTHER_EXPENSES,  # Finance processing step.
}  # Continue structured finance payload.


#: Well-known Chart-of-Accounts codes the Phase-4 services resolve by code. Kept here
#: (not hard-coded in services) so an entity with a customised chart can be remapped in
#: one place. All are seeded by :mod:`vs_finance.seed`.
PPE_ACCOUNT_CODE = "1500"                 # Property, Plant & Equipment (asset)
ACCUM_DEPRECIATION_CODE = "1900"          # Accumulated depreciation (contra-asset)
ACCRUED_REIMBURSEMENT_CODE = "2400"       # Staff expense-claim liability
PETTY_CASH_CODE = "1110"                  # Petty cash float (asset, child of 1100)
OUTPUT_VAT_CODE = "2200"                  # Output VAT payable (liability) — sales collect here
INPUT_VAT_CODE = "1300"                   # Input VAT recoverable (asset) — purchases offset here
WHT_PAYABLE_CODE = "2300"                 # Withholding-tax payable (liability)
PAYE_PAYABLE_CODE = "2310"                # PAYE (employee income tax) payable
PENSION_PAYABLE_CODE = "2320"             # Pension payable
NET_WAGES_PAYABLE_CODE = "2330"           # Net wages payable (cleared on disbursement)
SALARIES_EXPENSE_CODE = "5200"            # Salaries & wages expense
DEPRECIATION_EXPENSE_CODE = "5400"        # Depreciation expense
BANK_CHARGES_CODE = "5500"               # Bank charges expense
RETAINED_EARNINGS_CODE = "3200"          # Retained earnings (equity) — net income closes here
OPERATING_REVENUE_CODE = "4100"          # Operating revenue (income) — generic revenue line
CASH_BANK_CODE = "1100"                  # Cash & bank (the cash-flow statement's cash line)
SALES_RETURNS_CODE = "4900"              # Sales returns (contra-revenue) — credit notes default here
DISCOUNTS_ALLOWED_CODE = "4910"          # Discounts & allowances (contra-revenue) — concessions default here
BAD_DEBT_EXPENSE_CODE = "5300"           # Bad-debt / general expense — write-offs default here
CUSTOMER_CREDIT_CODE = "2140"            # Customer credit balances (liability) — overpayments / unapplied credit / refundable

#: Document-number prefix for the whole platform's finance documents (Code X Finance).
DOC_NUMBER_PREFIX = "CFX"  # Store intermediate finance value.

#: Reserved code for CodeX's own platform set of books (the operator's entity).
#: An uppercase identifier (like all entity codes); the display name is "CodeX".
PLATFORM_ENTITY_CODE = "CODEX"  # Store intermediate finance value.
