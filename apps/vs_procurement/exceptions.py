"""Domain exceptions for vs_procurement.

These subclass :class:`vs_finance.exceptions.FinanceError` (via ``PostingError``) so
the finance posting/rejection plumbing treats them uniformly: the service wrappers
catch ``FinanceError``, write a durable rejection audit row, and re-raise. They carry
a typed ``error_code`` like every platform exception.
"""
from __future__ import annotations

from vs_finance.exceptions import FinanceError, PostingError


class ProcurementError(FinanceError):
    error_code = "PROCUREMENT_ERROR"
    default_message = "A procurement error occurred."


class RequisitionError(ProcurementError):
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


class ThreeWayMatchError(PostingError):
    """Raised when a vendor invoice fails the PO↔GRN↔invoice match and can't post."""
    error_code = "THREE_WAY_MATCH_FAILED"
    default_message = "The vendor invoice failed the three-way match."
    http_status = 409

    def __init__(self, match_status, message=None, **kwargs):
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
        self.code = code
        super().__init__(
            f"No {label or 'control'} account '{code}' found in this entity's chart of accounts.",
            code=code, **kwargs,
        )
