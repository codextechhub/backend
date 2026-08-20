"""Domain refusals for school onboarding.

Each carries the ``error_code`` / ``message`` / ``http_status`` triple that
``core.exceptions.custom_exception_handler`` renders into the platform
envelope, following ``vs_tenants.exceptions``. A view therefore never wraps a
service call in its own try/except: the service refuses, and the refusal
arrives at the client already shaped.

``extra`` is the handler's ``error.detail`` payload. Only two refusals carry
one, and both carry it because the client cannot act without it: the list of
tasks still blocking go-live, and the correlation reference for an activation
that failed.
"""
from __future__ import annotations


class OnboardingError(Exception):
    """Base for onboarding domain refusals."""

    error_code = "ONBOARDING_ERROR"
    default_message = "The onboarding action could not be completed."
    http_status = 400

    def __init__(self, message: str = ""):
        self.message = message or self.default_message
        super().__init__(self.message)


class OnboardingNotProvisioned(OnboardingError):
    """Raised when a tenant has no onboarding state at all.

    Deliberately 404 with a code of its own rather than an empty success
    payload: "nothing done yet" and "never set up" are different facts and the
    control room has to tell them apart.
    """

    error_code = "ONBOARDING_NOT_PROVISIONED"
    default_message = "Onboarding has not been set up for this school."
    http_status = 404


class OnboardingTenantNotSchool(OnboardingError):
    """Raised when provisioning is asked for a tenant that is not a school."""

    error_code = "ONBOARDING_TENANT_NOT_SCHOOL"
    default_message = "Onboarding can only be provisioned for a school tenant."
    http_status = 422


class InvalidTaskTransition(OnboardingError):
    """Raised when a task is moved along an edge the lifecycle disallows."""

    error_code = "INVALID_TASK_TRANSITION"
    http_status = 422

    def __init__(self, *, from_state: str, to_state: str):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"An onboarding task cannot move from {from_state} to {to_state}.",
        )


class TaskAlreadyInState(OnboardingError):
    """Raised when a caller asks for the status the task already holds."""

    error_code = "TASK_ALREADY_IN_STATE"
    http_status = 409

    def __init__(self, *, state: str):
        self.state = state
        super().__init__(f"This onboarding task is already {state}.")


class RequiredTaskNotSkippable(OnboardingError):
    """Raised when SKIPPED is asked for a task the catalog marks required.

    A product decision taken after FR-003 was first written. Skipping used to
    be allowed for a required task and simply kept go-live blocked, which read
    to a school as permission to leave the step undone. It is now refused
    outright. Optional tasks are unaffected and skip exactly as before.

    Deliberately its own code rather than INVALID_TASK_TRANSITION: the edge is
    perfectly legal, it is *this task* that may not travel it, and a client
    that told the school "you cannot skip from here" would be explaining the
    wrong thing.
    """

    error_code = "REQUIRED_TASK_NOT_SKIPPABLE"
    http_status = 422

    def __init__(self, *, key: str):
        self.key = key
        super().__init__(
            "This onboarding step is required and cannot be skipped.",
        )


class TaskConditionNotMet(OnboardingError):
    """Raised when DONE is requested but the platform can see it is not done.

    Only tasks with a machine-checkable condition can raise this. The ones with
    no backend to check against (academic structure) are completed on the
    school's word, which is stated plainly rather than hidden behind a stub.
    """

    error_code = "TASK_CONDITION_NOT_MET"
    http_status = 422

    def __init__(self, *, key: str, reason: str = ""):
        self.key = key
        super().__init__(
            reason or "This onboarding step is not complete yet.",
        )


class OnboardingAlreadyLive(OnboardingError):
    """Raised when anything tries to change onboarding state after go-live."""

    error_code = "ONBOARDING_ALREADY_LIVE"
    default_message = "This school is already live; onboarding is closed."
    http_status = 409


class OnboardingNotReady(OnboardingError):
    """Raised when go-live is requested before every required task is done."""

    error_code = "ONBOARDING_NOT_READY"
    http_status = 422

    def __init__(self, *, outstanding):
        self.outstanding = list(outstanding)
        self.extra = {"outstanding_required_tasks": self.outstanding}
        super().__init__("The school is not ready to go live.")


class GoLiveAlreadyPending(OnboardingError):
    """Raised on a second go-live request while one is still pending.

    The friendly path. The partial unique constraint
    ``uq_golive_one_pending_per_tenant`` is the guarantee, and this refusal is
    raised from both the pre-check and the integrity error it catches.
    """

    error_code = "GOLIVE_ALREADY_PENDING"
    default_message = "A go-live request for this school is already awaiting review."
    http_status = 409


class AcknowledgementRequired(OnboardingError):
    """Raised when the go-live acknowledgement is missing or false."""

    error_code = "ACKNOWLEDGEMENT_REQUIRED"
    default_message = (
        "You must acknowledge the go-live terms before submitting this request."
    )
    http_status = 422


class GoLiveNotPending(OnboardingError):
    """Raised when a request that is not PENDING is approved or rejected."""

    error_code = "GOLIVE_NOT_PENDING"
    default_message = "This go-live request has already been reviewed."
    http_status = 409


class RejectionReasonRequired(OnboardingError):
    """Raised when a rejection arrives without a reason."""

    error_code = "REJECTION_REASON_REQUIRED"
    default_message = "A reason is required when rejecting a go-live request."
    http_status = 422


class OnboardingNotSuspended(OnboardingError):
    """Raised when reinstatement is asked for a school that is not suspended.

    Reinstatement is the answer to expiry and to nothing else. A pending school
    is already onboarding and a live one finished; neither has anything to be
    put back to, and treating the call as a no-op would let an operator believe
    they had restarted a clock they had not touched.
    """

    error_code = "ONBOARDING_NOT_SUSPENDED"
    default_message = (
        "This school is not suspended, so there is nothing to reinstate."
    )
    http_status = 409


class GoLiveActivationFailed(OnboardingError):
    """Raised when activation fails after approval.

    Everything the activation touched is rolled back, so the school is never
    half live. The reference is the correlation id written onto the request row
    and into the audit trail, and it is the only thing the caller can quote to
    support.
    """

    error_code = "GOLIVE_ACTIVATION_FAILED"
    http_status = 500

    def __init__(self, *, failure_reference: str):
        self.failure_reference = failure_reference
        self.extra = {"failure_reference": failure_reference}
        super().__init__(
            "Activation could not be completed. The school was not changed.",
        )
