"""Domain exceptions for vs_tenants.

Each carries the typed ``error_code`` / ``message`` pair that
``core.exceptions.custom_exception_handler`` renders into the platform
envelope, so a service can refuse an operation without every calling view
wrapping it in its own try/except.

These arrived here with ``Branch``. They describe a *site* lifecycle, not a
school one: a clinic chain or a retail group refuses the same edges for the
same reasons, and ``Branch.transition`` raises them, so leaving them in the
school app would have meant a platform model importing a product app.
"""
from __future__ import annotations


class TenantsError(Exception):
    """Base for tenant/site domain refusals."""

    error_code = "TENANTS_ERROR"
    default_message = "The requested operation could not be completed."
    http_status = 400

    def __init__(self, message: str = ""):
        self.message = message or self.default_message
        super().__init__(self.message)


class BranchLifecycleError(TenantsError):
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


class TenantNotLive(TenantsError):
    """Raised when a PENDING tenant reaches a surface that is not open to it.

    A tenant that has not gone live authenticates normally, so the caller is
    who they say they are and owns the tenant they asserted. What they lack is
    a live tenant, not a permission - hence 403 with a code of its own, and
    deliberately not 404: 404 is reserved for a caller asserting a tenant that
    is not theirs, where even the existence of the tenant must stay hidden.
    """

    error_code = "TENANT_NOT_LIVE"
    default_message = (
        "This school is still being set up. Complete onboarding and go live to "
        "use this part of the platform."
    )
    http_status = 403
