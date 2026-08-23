"""Domain exceptions for vs_procurement.

These subclass :class:`vs_finance.exceptions.FinanceError` (via ``PostingError``) so
the finance posting/rejection plumbing treats them uniformly: the service wrappers
catch ``FinanceError``, write a durable rejection audit row, and re-raise. They carry
a typed ``error_code`` like every platform exception.
"""
from __future__ import annotations

from vs_finance.exceptions import FinanceError, PostingError


class ProcurementError(FinanceError):
    """Base for non-posting procurement validation and lifecycle failures."""

    error_code = "PROCUREMENT_ERROR"
    default_message = "A procurement error occurred."


class RequisitionError(ProcurementError):
    """Raised when requisition creation, submission, or conversion is invalid."""

    error_code = "REQUISITION_ERROR"
    default_message = "The requisition could not be processed."


class SourcingError(ProcurementError):
    """Raised for RFQ / vendor-quotation lifecycle violations (issue, submit, award)."""
    error_code = "SOURCING_ERROR"
    default_message = "The sourcing action could not be completed."


class ContractError(ProcurementError):
    """Raised for vendor-contract lifecycle violations (activate, renew, terminate)."""
    error_code = "CONTRACT_ERROR"
    default_message = "The contract action could not be completed."


class ApprovalWorkflowError(ProcurementError):
    """Raised for procurement spend-approval / vs_workflow hand-off violations."""
    error_code = "APPROVAL_WORKFLOW_ERROR"
    default_message = "The approval action could not be completed."
    http_status = 409


class ApprovalTemplateMissingError(ApprovalWorkflowError):
    """Raised when no approval template exists for a document type being submitted.

    Deliberately distinct from "a template exists but nobody currently holds the
    approving permission". The second case is not an error: the document is
    submitted and parks on its stage until an approver is granted the permission.
    Only *this* case is a configuration failure the submitter must be told about,
    and the engine's own ``TemplateNotFoundError`` message names internal template
    codes and document-type tokens, so it is translated here rather than surfaced.
    """
    error_code = "APPROVAL_TEMPLATE_MISSING"
    default_message = "No approval route is configured for this document."


class ApprovalOverrideError(ApprovalWorkflowError):
    """Base for refusals of the parked-approval override (see ``approval_override``).

    Separate from :class:`ApprovalWorkflowError` so a caller (or an alerting rule) can
    tell "the override was refused" apart from any other approval hand-off failure -
    the override is a break-glass control and every refusal of it is worth seeing.
    """
    error_code = "APPROVAL_OVERRIDE_REFUSED"
    default_message = "The approval override could not be applied."


class ApprovalNotParkedError(ApprovalOverrideError):
    """Raised when an override is attempted on a document somebody can still decide.

    The override exists solely to free a document nobody is *able* to act on. If a
    single eligible approver exists - including one who has only just been granted the
    permission, which the parking repair discovers first - the answer is a decision,
    not an override.
    """
    error_code = "APPROVAL_NOT_PARKED"
    default_message = "This document is not parked, so it cannot be released by override."


class ApprovalOverrideReasonError(ApprovalOverrideError):
    """Raised when the mandatory override reason is missing, blank, or unusable.

    One error covers the whole reason contract (present, textual, bounded) because to a
    caller they are the same fix: send a real justification.
    """
    error_code = "APPROVAL_OVERRIDE_REASON_INVALID"
    default_message = "A written reason is required to release an approval by override."
    http_status = 400


class ApprovalOverrideNotPermittedError(ApprovalOverrideError):
    """Raised when the actor does not hold the dedicated override permission."""
    error_code = "APPROVAL_OVERRIDE_FORBIDDEN"
    default_message = "You do not have permission to release an approval by override."
    http_status = 403


class StockError(PostingError):
    """Raised for stock-ledger violations (issue, adjustment, valuation)."""
    error_code = "STOCK_ERROR"
    default_message = "The stock action could not be completed."
    http_status = 409


class InsufficientStockError(StockError):
    """Raised when an issue would drive on-hand quantity below zero."""
    error_code = "INSUFFICIENT_STOCK"
    default_message = "Not enough stock on hand for this issue."

    def __init__(self, *, item_code="", requested=None, on_hand=None, **kwargs):
        """Attach machine-readable requested/on-hand quantities to the error payload."""
        self.item_code = item_code
        # String conversion keeps Decimal quantities JSON-safe for the shared exception handler.
        super().__init__(
            f"Cannot issue {requested} of '{item_code}': only {on_hand} on hand.",
            item_code=item_code, requested=str(requested), on_hand=str(on_hand),
            **kwargs,
        )


class ThreeWayMatchError(PostingError):
    """Raised when a vendor invoice fails the PO↔GRN↔invoice match and can't post."""
    error_code = "THREE_WAY_MATCH_FAILED"
    default_message = "The vendor invoice failed the three-way match."
    http_status = 409

    def __init__(self, match_status, message=None, **kwargs):
        """Preserve the exact match outcome for API clients and rejection audits."""
        self.match_status = match_status
        super().__init__(
            message or f"Vendor invoice match status is '{match_status}'; cannot post.",
            match_status=str(match_status), **kwargs,
        )


class MissingControlAccountError(PostingError):
    """A required control account (GR/IR clearing, WHT payable, AP) is not configured."""
    error_code = "CONTROL_ACCOUNT_MISSING"
    default_message = "A required control account is not configured for this entity."

    def __init__(self, code, *, label="", **kwargs):
        """Identify the missing chart code without leaking any cross-entity account data."""
        self.code = code
        super().__init__(
            f"No {label or 'control'} account '{code}' found in this entity's chart of accounts.",
            code=code, **kwargs,
        )
