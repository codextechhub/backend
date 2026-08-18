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


class MainBranchCannotLeaveService(BranchLifecycleError):
    """Raised when the tenant's main branch is taken out of service.

    ``is_main`` marks the canonical site and exactly one row per tenant may
    carry it. Nothing hands the flag over on its own, so letting the main
    branch go SUSPENDED, INACTIVE or CLOSED leaves every reader of
    ``School.main_branch`` pointing at a site nobody may be posted to - and
    for CLOSED, permanently, because CLOSED is terminal and the partial unique
    index then refuses to make any survivor main.

    The message names the way out, which is :meth:`Branch.promote_to_main`
    over the API's ``is_main`` field.
    """

    error_code = "MAIN_BRANCH_CANNOT_LEAVE_SERVICE"

    def __init__(self, *, branch_name: str = "", to_state: str = ""):
        self.to_state = to_state
        subject = f"'{branch_name}'" if branch_name else "This branch"
        super().__init__(
            f"{subject} is the main branch. Make another branch the main "
            f"branch first, then take this one out of service."
        )


class LastBranchCannotLeaveService(BranchLifecycleError):
    """Raised when a tenant's only branch is taken out of service.

    Every school has at least one branch and there is nothing to hand the main
    flag to, so the advice given by :class:`MainBranchCannotLeaveService`
    cannot be followed. Winding a school down is a school-level action, not a
    branch-level one, so that is what the message points at.
    """

    error_code = "LAST_BRANCH_CANNOT_LEAVE_SERVICE"

    def __init__(self, *, branch_name: str = "", to_state: str = ""):
        self.to_state = to_state
        subject = f"'{branch_name}'" if branch_name else "This branch"
        super().__init__(
            f"{subject} is the only branch, and every school must keep one in "
            f"service. Deactivate the school itself instead."
        )


class BranchNotInService(BranchLifecycleError):
    """Raised when an out-of-service branch is asked to become the main one.

    Promoting a suspended or closed branch would rebuild by hand exactly the
    dead end the guards above exist to prevent.
    """

    error_code = "BRANCH_NOT_IN_SERVICE"

    def __init__(self, *, branch_name: str = "", status: str = ""):
        self.status = status
        subject = f"'{branch_name}'" if branch_name else "This branch"
        super().__init__(
            f"{subject} is {status or 'out of service'} and cannot become the "
            f"main branch. Bring it back into service first."
        )


class TenantSlugFrozen(TenantsError):
    """Raised when a tenant that has gone live is asked to move its slug.

    The slug is the sign-in address (``bright-star.xvs.codexng.com``), so it
    stops being editable the moment the first family is told where to sign in.
    :meth:`Tenant._assert_slug_unchanged_once_live` and
    :meth:`School._check_slug_change` are the backstops and raise Django's own
    ``ValidationError``, because they run inside ``save()``/``full_clean()``
    where nothing else would be caught. This is what an API caller gets: the
    same refusal, hoisted into the request layer so the rejection carries a
    status of its own instead of arriving as a field error on a write that
    could never have been attempted.

    409 rather than 400, alongside :class:`BranchLifecycleError`: the payload
    is well-formed and would have been accepted yesterday. What refuses it is
    the school's current state.
    """

    error_code = "TENANT_SLUG_FROZEN"
    http_status = 409

    def __init__(self, *, tenant_name: str = "", slug: str = ""):
        self.slug = slug
        subject = f"'{tenant_name}'" if tenant_name else "This school"
        address = f" at '{slug}'" if slug else ""
        super().__init__(
            f"{subject} is live{address}, so its address cannot move. Changing "
            f"it would break every link and sign-in its users already have."
        )


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
