"""Domain exceptions for vs_schools.

Each carries the typed ``error_code`` / ``message`` pair that
``core.exceptions.custom_exception_handler`` renders into the platform
envelope, so a service can refuse an operation without every calling view
wrapping it in its own try/except.
"""
from __future__ import annotations


class SchoolsError(Exception):
    """Base for school/branch domain refusals."""

    error_code = "SCHOOLS_ERROR"
    default_message = "The requested school operation could not be completed."
    http_status = 400

    def __init__(self, message: str = ""):
        self.message = message or self.default_message
        super().__init__(self.message)


class BranchLifecycleError(SchoolsError):
    """Base for branch status-transition refusals.

    These are conflicts with the branch's current state rather than malformed
    input, so they answer 409 rather than 400.
    """

    error_code = "BRANCH_LIFECYCLE_ERROR"
    default_message = "The branch lifecycle action could not be completed."
    http_status = 409


class InvalidBranchTransition(BranchLifecycleError):
    """Raised when a branch is moved along an edge the lifecycle disallows."""

    error_code = "INVALID_BRANCH_TRANSITION"

    def __init__(self, *, from_state: str, to_state: str):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"A branch cannot move from {from_state} to {to_state}."
        )


class BranchAlreadyInState(BranchLifecycleError):
    """Raised when a caller asks for the state the branch is already in."""

    error_code = "BRANCH_ALREADY_IN_STATE"

    def __init__(self, *, state: str):
        self.state = state
        super().__init__(f"Branch is already {state}.")
