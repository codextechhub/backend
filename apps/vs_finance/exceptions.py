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
