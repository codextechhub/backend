"""Domain exceptions for vs_finance. All engine errors carry a typed ``error_code``,
matching the platform convention (see ``vs_workflow.exceptions`` and the duck-typed
``core.exceptions.custom_exception_handler``, which renders ``error_code`` +
``message`` + ``extra`` at ``http_status``).
"""


class FinanceError(Exception):
    error_code = "FINANCE_ERROR"
    default_message = "A finance error occurred."
    http_status = 422

    def __init__(self, message=None, **kwargs):
        self.message = message or self.default_message
        self.extra = kwargs
        super().__init__(self.message)


class PostingError(FinanceError):
    error_code = "POSTING_ERROR"
    default_message = "The journal could not be posted."


class UnbalancedJournalError(PostingError):
    error_code = "JOURNAL_UNBALANCED"
    default_message = "Journal debits and credits do not balance."

    def __init__(self, debit, credit, **kwargs):
        self.debit = debit
        self.credit = credit
        super().__init__(
            f"Journal does not balance: debits={debit} kobo != credits={credit} kobo "
            f"(difference {debit - credit} kobo).",
            debit=debit, credit=credit, difference=debit - credit, **kwargs,
        )


class PeriodClosedError(PostingError):
    error_code = "PERIOD_CLOSED"
    default_message = "Cannot post into a closed or locked period."
    http_status = 409

    def __init__(self, period_label, status, **kwargs):
        self.period_label = period_label
        self.status = status
        super().__init__(
            f"Cannot post into period '{period_label}': it is '{status}'. "
            f"Re-open the period or post into the current open period.",
            period_label=period_label, status=status, **kwargs,
        )


class InactiveAccountError(PostingError):
    error_code = "ACCOUNT_INACTIVE"
    default_message = "The account is inactive or not postable."

    def __init__(self, account_code, **kwargs):
        self.account_code = account_code
        super().__init__(
            f"Account '{account_code}' is inactive or not postable.",
            account_code=account_code, **kwargs,
        )


class DocumentNumberingError(FinanceError):
    error_code = "DOCUMENT_NUMBERING_FAILED"
    default_message = "Could not allocate a document number."


# --------------------------------------------------------------------------- #
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close      #
# --------------------------------------------------------------------------- #

class MissingAccountError(PostingError):
    """A well-known control account (by CoA code) is absent or not postable."""

    error_code = "ACCOUNT_NOT_FOUND"
    default_message = "A required control account is missing from the chart."

    def __init__(self, code, label="", **kwargs):
        self.code = code
        self.label = label
        super().__init__(
            f"Required account '{code}'{f' ({label})' if label else ''} is missing, "
            f"inactive or not postable for this entity.",
            code=code, label=label, **kwargs,
        )


class BankReconciliationError(FinanceError):
    error_code = "BANK_RECONCILIATION_ERROR"
    default_message = "The bank statement line could not be reconciled."


class ExpenseClaimError(PostingError):
    error_code = "EXPENSE_CLAIM_ERROR"
    default_message = "The expense claim could not be processed."


class PayrollError(PostingError):
    error_code = "PAYROLL_ERROR"
    default_message = "The payroll run could not be processed."


class BudgetError(FinanceError):
    error_code = "BUDGET_ERROR"
    default_message = "The budget could not be processed."


class DepreciationError(PostingError):
    error_code = "DEPRECIATION_ERROR"
    default_message = "Depreciation could not be processed."


class PeriodCloseError(FinanceError):
    error_code = "PERIOD_CLOSE_ERROR"
    default_message = "The period could not be closed."
    http_status = 409

    def __init__(self, message=None, *, failures=None, **kwargs):
        self.failures = failures or []
        super().__init__(message, failures=self.failures, **kwargs)
