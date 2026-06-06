"""Shared enumerations and constants for the finance engine.

Defined once here so Phase-1 models (Account, FiscalPeriod, JournalEntry …) and the
posting service agree on the same vocabulary without circular imports.
"""
from __future__ import annotations

from django.db import models


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


class DocType(models.TextChoices):
    """Document-type tokens used by the numbering sequence.

    The token becomes the middle segment of a document number, e.g. ``INV`` in
    ``CFX-B01-INV-2026-00821``. Keep tokens short, uppercase and stable — they are
    persisted inside human-facing identifiers.
    """
    JOURNAL = "JNL", "Journal Entry"
    INVOICE = "INV", "Sales / AR Invoice"
    RECEIPT = "RCP", "Receipt"
    PAYMENT = "PAY", "Payment"
    CREDIT_NOTE = "CRN", "Credit Note"
    PURCHASE_REQUISITION = "PR", "Purchase Requisition"
    RFQ = "RFQ", "Request for Quotation"
    PURCHASE_ORDER = "PO", "Purchase Order"
    GOODS_RECEIVED = "GRN", "Goods Received Note"
    VENDOR_INVOICE = "VIN", "Vendor Invoice"
    VENDOR_PAYMENT = "VPY", "Vendor Payment"
    EXPENSE_CLAIM = "EXP", "Expense Claim"
    PAYROLL_RUN = "PYR", "Payroll Run"
    FIXED_ASSET = "FA", "Fixed Asset"

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


class NormalBalance(models.TextChoices):
    """The side on which an account normally carries its balance."""
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


#: Default natural balance for each account root (before any contra flip).
NORMAL_BALANCE_BY_TYPE = {
    AccountType.ASSET: NormalBalance.DEBIT,
    AccountType.EXPENSE: NormalBalance.DEBIT,
    AccountType.LIABILITY: NormalBalance.CREDIT,
    AccountType.EQUITY: NormalBalance.CREDIT,
    AccountType.INCOME: NormalBalance.CREDIT,
}


class JournalSource(models.TextChoices):
    """Where a journal entry originated — for filtering and audit, not for posting logic.

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


class InvoiceSource(models.TextChoices):
    """What generated an invoice — keeps the AR core domain-neutral.

    The invoice model is generic; ``source`` records the originating mechanism so a
    school-fee run, a subscription engine or an API caller can all emit the *same*
    generic :class:`~vs_finance.models.Invoice` without the ledger knowing about any
    of them. Student/fee concepts live only in the adapter that sets ``FEE_BILLING``.
    """
    MANUAL = "MANUAL", "Manual"
    FEE_BILLING = "FEE_BILLING", "Fee Billing"
    SUBSCRIPTION = "SUBSCRIPTION", "Subscription"
    API = "API", "API"


class InvoicePaymentStatus(models.TextChoices):
    """How much of an invoice has been settled — distinct from its document status.

    Document ``status`` (DRAFT→POSTED→…) tracks the *ledger* lifecycle; this tracks
    *cash* against the invoice and is derived from amount paid vs total.
    """
    UNPAID = "UNPAID", "Unpaid"
    PARTIAL = "PARTIAL", "Partially Paid"
    PAID = "PAID", "Paid"


class PaymentMethod(models.TextChoices):
    """How a customer receipt was tendered (operational detail, not posting logic)."""
    CASH = "CASH", "Cash"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    CARD = "CARD", "Card"
    CHEQUE = "CHEQUE", "Cheque"
    ONLINE = "ONLINE", "Online / Gateway"
    OTHER = "OTHER", "Other"


# --------------------------------------------------------------------------- #
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close     #
# --------------------------------------------------------------------------- #

class BankLineStatus(models.TextChoices):
    """Reconciliation state of an imported bank-statement line.

    UNMATCHED -> not yet paired with a ledger movement.
    MATCHED   -> reconciled to a GL bank-account journal line.
    IGNORED   -> intentionally excluded (duplicate, opening line, etc.).
    """
    UNMATCHED = "UNMATCHED", "Unmatched"
    MATCHED = "MATCHED", "Matched"
    IGNORED = "IGNORED", "Ignored"


class PayrollRunStatus(models.TextChoices):
    """Lifecycle of a payroll run (a batch of employee pay lines)."""
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted (accrued)"
    PAID = "PAID", "Paid (disbursed)"
    CANCELLED = "CANCELLED", "Cancelled"


class BudgetStatus(models.TextChoices):
    """Lifecycle of a budget; locked on approval so actuals can't be re-planned."""
    DRAFT = "DRAFT", "Draft"
    APPROVED = "APPROVED", "Approved"
    LOCKED = "LOCKED", "Locked"


class DepreciationMethod(models.TextChoices):
    """Depreciation method for a fixed asset. Straight-line only for now."""
    STRAIGHT_LINE = "STRAIGHT_LINE", "Straight line"


class AssetStatus(models.TextChoices):
    """Lifecycle of a fixed asset in the register."""
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    FULLY_DEPRECIATED = "FULLY_DEPRECIATED", "Fully depreciated"
    DISPOSED = "DISPOSED", "Disposed"


class FinanceAuditAction(models.TextChoices):
    """Auditable finance actions recorded in the in-app, append-only audit log.

    The ledger itself (immutable posted/reversed journals + period locks) is the
    primary financial audit trail; this enum names the *actions around* it that the
    journals can't capture on their own — who pressed post, rejected attempts, period
    state changes and master-data edits.
    """
    JOURNAL_POSTED = "JOURNAL_POSTED", "Journal posted"
    JOURNAL_REVERSED = "JOURNAL_REVERSED", "Journal reversed"
    JOURNAL_POST_REJECTED = "JOURNAL_POST_REJECTED", "Journal posting rejected"
    INVOICE_POSTED = "INVOICE_POSTED", "Invoice posted"
    INVOICE_CANCELLED = "INVOICE_CANCELLED", "Invoice cancelled"
    PAYMENT_POSTED = "PAYMENT_POSTED", "Payment posted"
    PAYMENT_ALLOCATED = "PAYMENT_ALLOCATED", "Payment allocated"
    PERIOD_CLOSED = "PERIOD_CLOSED", "Period closed"
    PERIOD_REOPENED = "PERIOD_REOPENED", "Period re-opened"
    ACCOUNT_CREATED = "ACCOUNT_CREATED", "Account created"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED", "Account updated"
    # Procure-to-Pay. The vendor/PO/GRN documents live in vs_procurement,
    # but their audit vocabulary belongs to finance's authoritative log (finance does
    # not import procurement — these are just string constants).
    REQUISITION_APPROVED = "REQUISITION_APPROVED", "Requisition approved"
    PURCHASE_ORDER_APPROVED = "PURCHASE_ORDER_APPROVED", "Purchase order approved"
    GRN_POSTED = "GRN_POSTED", "Goods receipt posted"
    GRN_POST_REJECTED = "GRN_POST_REJECTED", "Goods receipt posting rejected"
    VENDOR_INVOICE_MATCHED = "VENDOR_INVOICE_MATCHED", "Vendor invoice matched"
    VENDOR_INVOICE_POSTED = "VENDOR_INVOICE_POSTED", "Vendor invoice posted"
    VENDOR_INVOICE_POST_REJECTED = "VENDOR_INVOICE_POST_REJECTED", "Vendor invoice posting rejected"
    VENDOR_PAYMENT_POSTED = "VENDOR_PAYMENT_POSTED", "Vendor payment posted"
    VENDOR_PAYMENT_POST_REJECTED = "VENDOR_PAYMENT_POST_REJECTED", "Vendor payment posting rejected"
    VENDOR_PAYMENT_ALLOCATED = "VENDOR_PAYMENT_ALLOCATED", "Vendor payment allocated"
    # Phase 4 — banking, expenses, payroll, budget, fixed assets, period close.
    BANK_RECONCILED = "BANK_RECONCILED", "Bank statement reconciled"
    BANK_CHARGE_POSTED = "BANK_CHARGE_POSTED", "Bank charge posted"
    EXPENSE_CLAIM_POSTED = "EXPENSE_CLAIM_POSTED", "Expense claim posted"
    EXPENSE_CLAIM_POST_REJECTED = "EXPENSE_CLAIM_POST_REJECTED", "Expense claim posting rejected"
    EXPENSE_CLAIM_SETTLED = "EXPENSE_CLAIM_SETTLED", "Expense claim settled"
    PAYROLL_POSTED = "PAYROLL_POSTED", "Payroll run posted"
    PAYROLL_POST_REJECTED = "PAYROLL_POST_REJECTED", "Payroll run posting rejected"
    PAYROLL_PAID = "PAYROLL_PAID", "Payroll run disbursed"
    BUDGET_APPROVED = "BUDGET_APPROVED", "Budget approved"
    ASSET_ACQUIRED = "ASSET_ACQUIRED", "Fixed asset acquired"
    DEPRECIATION_POSTED = "DEPRECIATION_POSTED", "Depreciation posted"
    PERIOD_LOCKED = "PERIOD_LOCKED", "Period locked"


class FinanceAuditStatus(models.TextChoices):
    """Outcome of an audited action."""
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


#: Well-known Chart-of-Accounts codes the Phase-4 services resolve by code. Kept here
#: (not hard-coded in services) so an entity with a customised chart can be remapped in
#: one place. All are seeded by :mod:`vs_finance.seed`.
PPE_ACCOUNT_CODE = "1500"                 # Property, Plant & Equipment (asset)
ACCUM_DEPRECIATION_CODE = "1900"          # Accumulated depreciation (contra-asset)
ACCRUED_REIMBURSEMENT_CODE = "2400"       # Staff expense-claim liability
PAYE_PAYABLE_CODE = "2310"                # PAYE (employee income tax) payable
PENSION_PAYABLE_CODE = "2320"             # Pension payable
NET_WAGES_PAYABLE_CODE = "2330"           # Net wages payable (cleared on disbursement)
SALARIES_EXPENSE_CODE = "5200"            # Salaries & wages expense
DEPRECIATION_EXPENSE_CODE = "5400"        # Depreciation expense
BANK_CHARGES_CODE = "5500"               # Bank charges expense
RETAINED_EARNINGS_CODE = "3200"          # Retained earnings (equity) — net income closes here
CASH_BANK_CODE = "1100"                  # Cash & bank (the cash-flow statement's cash line)

#: Document-number prefix for the whole platform's finance documents (Code X Finance).
DOC_NUMBER_PREFIX = "CFX"

#: Reserved code for CodeX's own platform set of books (the operator's entity).
#: An uppercase identifier (like all entity codes); the display name is "CodeX".
PLATFORM_ENTITY_CODE = "CODEX"
