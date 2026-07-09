"""Domain exceptions for vs_finance.  # Finance-layer exception hierarchy.

All engine errors carry a typed ``error_code``, matching the platform convention (see
``vs_workflow.exceptions`` and the duck-typed ``core.exceptions.custom_exception_handler``,
which renders ``error_code`` + ``message`` + ``extra`` at ``http_status``).  # Keep errors machine-readable across the stack.
"""


class FinanceError(Exception):  # Define the class used by this module.
    error_code = "FINANCE_ERROR"  # Generic finance-layer error code.
    default_message = "A finance error occurred."  # Default message when none is supplied.
    http_status = 422  # Finance validation/posting failures map to unprocessable entity.

    def __init__(self, message=None, **kwargs):  # Define the callable used by this module.
        self.message = message or self.default_message  # Store the user-facing message.
        self.extra = kwargs  # Preserve structured context for the exception handler.
        super().__init__(self.message)  # Initialize the base Exception with the message.


class PostingError(FinanceError):  # Define the class used by this module.
    error_code = "POSTING_ERROR"  # General journal-posting error.
    default_message = "The journal could not be posted."  # Default posting failure message.


class UnbalancedJournalError(PostingError):  # Define the class used by this module.
    error_code = "JOURNAL_UNBALANCED"  # Journal debits and credits do not balance.
    default_message = "Journal debits and credits do not balance."  # Default balancing error message.

    def __init__(self, debit, credit, **kwargs):  # Define the callable used by this module.
        self.debit = debit  # Store the debit total for handlers/logging.
        self.credit = credit  # Store the credit total for handlers/logging.
        super().__init__(  # Build a detailed imbalance message.
            f"Journal does not balance: debits={debit} kobo != credits={credit} kobo "
            f"(difference {debit - credit} kobo).",
            debit=debit, credit=credit, difference=debit - credit, **kwargs,  # Continue the structured value.
        )  # Close the grouped expression.


class PeriodClosedError(PostingError):  # Define the class used by this module.
    error_code = "PERIOD_CLOSED"  # Accounting period is closed or locked.
    default_message = "Cannot post into a closed or locked period."  # Default closed-period message.
    http_status = 409  # Closed period is a conflict, not a validation error.

    def __init__(self, period_label, status, **kwargs):  # Define the callable used by this module.
        self.period_label = period_label  # Store the period label for diagnostics.
        self.status = status  # Store the period status for diagnostics.
        super().__init__(  # Build a detailed closed-period message.
            f"Cannot post into period '{period_label}': it is '{status}'. "
            f"Re-open the period or post into the current open period.",
            period_label=period_label, status=status, **kwargs,  # Continue the structured value.
        )  # Close the grouped expression.


class InactiveAccountError(PostingError):  # Define the class used by this module.
    error_code = "ACCOUNT_INACTIVE"  # Account cannot accept postings.
    default_message = "The account is inactive or not postable."  # Default inactive-account message.

    def __init__(self, account_code, **kwargs):  # Define the callable used by this module.
        self.account_code = account_code  # Store the account code for diagnostics.
        super().__init__(  # Build a concise inactive-account error message.
            f"Account '{account_code}' is inactive or not postable.",
            account_code=account_code, **kwargs,  # Continue the structured value.
        )  # Close the grouped expression.


class DocumentNumberingError(FinanceError):  # Define the class used by this module.
    error_code = "DOCUMENT_NUMBERING_FAILED"  # Sequence allocation failed.
    default_message = "Could not allocate a document number."  # Default numbering failure message.


# --------------------------------------------------------------------------- #  # Finance phase-4 support errors below.
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close      #  # Support modules share these errors.
# --------------------------------------------------------------------------- #  # End phase-4 header.

class MissingAccountError(PostingError):  # Define the class used by this module.
    """A well-known control account (by CoA code) is absent or not postable."""

    error_code = "ACCOUNT_NOT_FOUND"  # Required control account could not be resolved.
    default_message = "A required control account is missing from the chart."  # Default missing-account message.

    def __init__(self, code, label="", **kwargs):
        self.code = code  # Store the missing account code.
        self.label = label  # Store the human-readable label, if any.
        super().__init__(  # Build a detailed missing-account message.
            f"Required account '{code}'{f' ({label})' if label else ''} is missing, "
            f"inactive or not postable for this entity.",
            code=code, label=label, **kwargs,  # Continue the structured value.
        )  # Close the grouped expression.


class BankReconciliationError(FinanceError):  # Define the class used by this module.
    error_code = "BANK_RECONCILIATION_ERROR"  # Statement reconciliation failure.
    default_message = "The bank statement line could not be reconciled."  # Default reconciliation message.


class ExpenseClaimError(PostingError):  # Define the class used by this module.
    error_code = "EXPENSE_CLAIM_ERROR"  # Expense claim processing failure.
    default_message = "The expense claim could not be processed."  # Default claim failure message.


class PettyCashError(PostingError):  # Define the class used by this module.
    """Raised for petty-cash fund / voucher lifecycle violations."""
    error_code = "PETTY_CASH_ERROR"  # Petty cash lifecycle error.
    default_message = "The petty cash action could not be completed."  # Default petty cash message.
    http_status = 409  # Petty cash lifecycle violations are conflicts.


class PettyCashOverdrawError(PettyCashError):  # Define the class used by this module.
    """Raised when a voucher would drive the fund's on-hand cash below zero."""
    error_code = "PETTY_CASH_OVERDRAWN"  # Fund lacks enough cash for the voucher.
    default_message = "The petty cash fund does not hold enough cash for this voucher."  # Default overdraw message.

    def __init__(self, *, fund_name="", requested=None, on_hand=None, **kwargs):
        self.fund_name = fund_name  # Store the fund name for diagnostics.
        super().__init__(  # Build an overdraw message with the requested and available amounts.
            f"Voucher of {requested} exceeds the '{fund_name}' fund's {on_hand} kobo on hand.",
            fund_name=fund_name, requested=str(requested), on_hand=str(on_hand),  # Continue the structured value.
            **kwargs,  # Continue the structured value.
        )  # Close the grouped expression.


class PayrollError(PostingError):  # Define the class used by this module.
    error_code = "PAYROLL_ERROR"  # Payroll processing failure.
    default_message = "The payroll run could not be processed."  # Default payroll message.


class TaxFilingError(PostingError):  # Define the class used by this module.
    """Raised for tax-remittance / filing lifecycle violations."""
    error_code = "TAX_FILING_ERROR"  # Tax filing or remittance lifecycle violation.
    default_message = "The tax filing action could not be completed."  # Default tax-filing message.
    http_status = 409  # Filing conflicts are state conflicts.


class BudgetError(FinanceError):  # Define the class used by this module.
    error_code = "BUDGET_ERROR"  # Budget processing failure.
    default_message = "The budget could not be processed."  # Default budget message.


class DepreciationError(PostingError):  # Define the class used by this module.
    error_code = "DEPRECIATION_ERROR"  # Fixed-asset depreciation failure.
    default_message = "Depreciation could not be processed."  # Default depreciation message.


class PeriodCloseError(FinanceError):  # Define the class used by this module.
    error_code = "PERIOD_CLOSE_ERROR"  # Period-close workflow failure.
    default_message = "The period could not be closed."  # Default close-period message.
    http_status = 409  # Closing conflicts are state conflicts.

    def __init__(self, message=None, *, failures=None, **kwargs):  # Define the callable used by this module.
        self.failures = failures or []  # Preserve the list of close failures for callers.
        super().__init__(message, failures=self.failures, **kwargs)  # Pass structured failure context upstream.
