"""Domain exceptions for vs_finance.

All engine errors carry a typed ``error_code``, matching the platform convention (see
``vs_workflow.exceptions`` and the duck-typed ``core.exceptions.custom_exception_handler``,
which renders ``error_code`` + ``message`` + ``extra`` at ``http_status``).  # Keep errors machine-readable across the stack.
"""


class FinanceError(Exception):
    error_code = "FINANCE_ERROR"  # Generic finance-layer error code.
    default_message = "A finance error occurred."  # Default message when none is supplied.
    http_status = 422  # Finance validation/posting failures map to unprocessable entity.

    # Initialize this object with its required state.
    def __init__(self, message=None, **kwargs):
        self.message = message or self.default_message  # Store the user-facing message.
        self.extra = kwargs  # Preserve structured context for the exception handler.
        super().__init__(self.message)  # Initialize the base Exception with the message.


class PostingError(FinanceError):
    error_code = "POSTING_ERROR"  # General journal-posting error.
    default_message = "The journal could not be posted."  # Default posting failure message.


class UnbalancedJournalError(PostingError):
    error_code = "JOURNAL_UNBALANCED"  # Journal debits and credits do not balance.
    default_message = "Journal debits and credits do not balance."  # Default balancing error message.

    # Initialize this object with its required state.
    def __init__(self, debit, credit, **kwargs):
        self.debit = debit  # Store the debit total for handlers/logging.
        self.credit = credit  # Store the credit total for handlers/logging.
        super().__init__(  # Build a detailed imbalance message.
            f"Journal does not balance: debits={debit} kobo != credits={credit} kobo "
            f"(difference {debit - credit} kobo).",
            debit=debit, credit=credit, difference=debit - credit, **kwargs,
        )


class PeriodClosedError(PostingError):
    error_code = "PERIOD_CLOSED"  # Accounting period is closed or locked.
    default_message = "Cannot post into a closed or locked period."  # Default closed-period message.
    http_status = 409  # Closed period is a conflict, not a validation error.

    # Initialize this object with its required state.
    def __init__(self, period_label, status, **kwargs):
        self.period_label = period_label  # Store the period label for diagnostics.
        self.status = status  # Store the period status for diagnostics.
        # "missing" means no period covers the date at all. The old advice ("choose
        # another date") is impossible when the real cause is that nobody created
        # next year's calendar, because then no date works, so name that cause and
        # the fix. See vs_finance.posting.fiscal_calendar_runway, which warns before
        # an entity gets here.
        message = (
            "No fiscal period covers this date. Either the date falls outside your "
            "fiscal calendar, or the next fiscal year has not been created yet. "
            "Open Fiscal Periods and create the next fiscal year if the calendar "
            "has run out."
            if status == "missing"
            else (
                f"Cannot post into period '{period_label}': it is '{status}'. "
                f"Re-open the period or post into the current open period."
            )
        )
        super().__init__(  # Build a detailed closed-period message.
            message,
            period_label=period_label, status=status, **kwargs,
        )


class BackdatedPostingError(PostingError):
    """A posting is dated before the thing it depends on existed.

    The period guards answer "may we book on this date at all?"; this one answers
    "could this transaction have happened on this date?". A refund cannot pay out
    credit that arrives a week later, a write-off cannot clear an invoice that has
    not been raised, a receipt cannot settle a future bill. See
    :mod:`vs_finance.chronology` for the single guard every service calls.
    """

    error_code = "POSTING_BACKDATED"  # Accounting date precedes the value it consumes.
    default_message = "This date is before the transaction it depends on."  # Default causality message.
    http_status = 409  # An impossible ordering is a conflict, not a field validation error.

    # Initialize this object with its required state.
    def __init__(self, *, subject, subject_date, source, source_date, remedy="", **kwargs):
        self.subject_date = subject_date  # Store the dependent document's date.
        self.source_date = source_date  # Store the date the depended-on value exists from.
        message = (
            f"{subject} is dated {subject_date}, but {source} only exists from "
            f"{source_date}. Nothing can be settled, refunded or written off before "
            f"the value it draws on exists."
        )
        if remedy:  # Append the caller's concrete way out when supplied.
            message = f"{message} {remedy}"
        super().__init__(  # Build the causality error with structured context.
            message,
            subject=subject, subject_date=str(subject_date),
            source=source, source_date=str(source_date), **kwargs,
        )


class InactiveAccountError(PostingError):
    error_code = "ACCOUNT_INACTIVE"  # Account cannot accept postings.
    default_message = "The account is inactive or not postable."  # Default inactive-account message.

    # Initialize this object with its required state.
    def __init__(self, account_code, **kwargs):
        self.account_code = account_code  # Store the account code for diagnostics.
        super().__init__(  # Build a concise inactive-account error message.
            f"Account '{account_code}' is inactive or not postable.",
            account_code=account_code, **kwargs,
        )


class PrimaryEntityExistsError(FinanceError):
    """Raised when a tenant is asked for a second TENANT-kind set of books.

    There is a real tension here, and it is deliberate, so please read this
    before "fixing" either side of it.

    :class:`~vs_finance.models.LedgerEntity` says in its own docstring that a
    single tenant may keep **several** entities, and that stays true: a group
    with subsidiaries, or a school running its own books beside
    platform-managed ones, is a legitimate arrangement. That is why this is a
    service-level rule and **not** a database constraint - the data model must
    keep allowing the shape.

    What cannot be allowed is a *second* entity of kind ``TENANT`` appearing by
    accident, because the finance abstraction layer resolves exactly one primary
    entity per tenant. Two candidates make that resolution ambiguous, and the
    ambiguity is only discovered later, by whichever posting picked the wrong
    set of books. So the extra entities a tenant may legitimately keep are
    PLATFORM, PRODUCT or OTHER, and a deliberate second TENANT entity has to be
    created by an operator who has decided that is what they want, not by an
    ordinary ``finance.entity.create`` call.

    The platform's own tenant is exempt: Codex keeps several sets of books for
    itself and is never resolved through the primary-entity lookup.
    """

    error_code = "PRIMARY_ENTITY_EXISTS"
    default_message = "This tenant already has a set of books."
    http_status = 409  # A conflict with existing state, not malformed input.

    def __init__(self, *, entity_code="", **kwargs):
        self.entity_code = entity_code
        super().__init__(
            f"This tenant already keeps a set of books"
            f"{f' ({entity_code})' if entity_code else ''}. A tenant has one "
            f"primary set of books; create any additional entity with an "
            f"explicit kind other than TENANT.",
            entity_code=entity_code, **kwargs,
        )


# Group behavior for Document Numbering Error.
class DocumentNumberingError(FinanceError):
    error_code = "DOCUMENT_NUMBERING_FAILED"  # Sequence allocation failed.
    default_message = "Could not allocate a document number."  # Default numbering failure message.


# --------------------------------------------------------------------------- #  # Finance phase-4 support errors below.
# Phase 4 - banking, expenses, payroll, budget, fixed assets, period close      #  # Support modules share these errors.
# --------------------------------------------------------------------------- #  # End phase-4 header.

# Group behavior for Missing Account Error.
class MissingAccountError(PostingError):
    """A well-known control account (by CoA code) is absent or not postable."""

    error_code = "ACCOUNT_NOT_FOUND"  # Required control account could not be resolved.
    default_message = "A required control account is missing from the chart."  # Default missing-account message.

    def __init__(self, code, label="", **kwargs):
        self.code = code  # Store the missing account code.
        self.label = label  # Store the human-readable label, if any.
        super().__init__(  # Build a detailed missing-account message.
            f"Required account '{code}'{f' ({label})' if label else ''} is missing, "
            f"inactive or not postable for this entity.",
            code=code, label=label, **kwargs,
        )


class BankReconciliationError(FinanceError):
    error_code = "BANK_RECONCILIATION_ERROR"  # Statement reconciliation failure.
    default_message = "The bank statement line could not be reconciled."  # Default reconciliation message.


class ExpenseClaimError(PostingError):
    error_code = "EXPENSE_CLAIM_ERROR"  # Expense claim processing failure.
    default_message = "The expense claim could not be processed."  # Default claim failure message.


class PettyCashError(PostingError):
    """Raised for petty-cash fund / voucher lifecycle violations."""
    error_code = "PETTY_CASH_ERROR"  # Petty cash lifecycle error.
    default_message = "The petty cash action could not be completed."  # Default petty cash message.
    http_status = 409  # Petty cash lifecycle violations are conflicts.


# Group behavior for Petty Cash Overdraw Error.
class PettyCashOverdrawError(PettyCashError):
    """Raised when a voucher would drive the fund's on-hand cash below zero."""
    error_code = "PETTY_CASH_OVERDRAWN"  # Fund lacks enough cash for the voucher.
    default_message = "The petty cash fund does not hold enough cash for this voucher."  # Default overdraw message.

    def __init__(self, *, fund_name="", requested=None, on_hand=None, **kwargs):
        self.fund_name = fund_name  # Store the fund name for diagnostics.
        super().__init__(  # Build an overdraw message with the requested and available amounts.
            f"Voucher of {requested} exceeds the '{fund_name}' fund's {on_hand} kobo on hand.",
            fund_name=fund_name, requested=str(requested), on_hand=str(on_hand),
            **kwargs,
        )


class PayrollError(PostingError):
    error_code = "PAYROLL_ERROR"  # Payroll processing failure.
    default_message = "The payroll run could not be processed."  # Default payroll message.


class TaxFilingError(PostingError):
    """Raised for tax-remittance / filing lifecycle violations."""
    error_code = "TAX_FILING_ERROR"  # Tax filing or remittance lifecycle violation.
    default_message = "The tax filing action could not be completed."  # Default tax-filing message.
    http_status = 409  # Filing conflicts are state conflicts.


class BudgetError(FinanceError):
    error_code = "BUDGET_ERROR"  # Budget processing failure.
    default_message = "The budget could not be processed."  # Default budget message.


class DepreciationError(PostingError):
    error_code = "DEPRECIATION_ERROR"  # Fixed-asset depreciation failure.
    default_message = "Depreciation could not be processed."  # Default depreciation message.


# Group behavior for Period Close Error.
class PeriodCloseError(FinanceError):
    error_code = "PERIOD_CLOSE_ERROR"  # Period-close workflow failure.
    default_message = "The period could not be closed."  # Default close-period message.
    http_status = 409  # Closing conflicts are state conflicts.

    # Initialize this object with its required state.
    def __init__(self, message=None, *, failures=None, **kwargs):
        self.failures = failures or []  # Preserve the list of close failures for callers.
        super().__init__(message, failures=self.failures, **kwargs)  # Pass structured failure context upstream.
