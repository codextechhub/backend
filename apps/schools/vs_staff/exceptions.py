"""Domain refusals, rendered by ``core.exceptions.custom_exception_handler``.

The handler reads exactly three attributes - ``error_code``, ``message`` and
``extra`` - and drops anything else, so a payload goes in ``extra`` or it never
reaches the caller.

Every message is written for the person reading it, because the design renders
a refusal verbatim under the control that caused it. Each one says what the
school still has to do rather than what the guard observed, and each one names
the thing rather than counting it: "3 classes need cover" sends a head teacher
hunting, and "JSS1 A Mathematics, JSS1 B Mathematics" does not.

FRD M12 v2.1 section 11.
"""
from __future__ import annotations


class StaffError(Exception):
    error_code = "STAFF_ERROR"
    default_message = "That could not be done to this staff record."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class InvalidStatusTransition(StaffError):
    """A move the employment lifecycle does not allow.

    Names both statuses, because "invalid transition" tells an administrator
    nothing about which of the two ends they got wrong.
    """

    error_code = "INVALID_STATUS_TRANSITION"
    default_message = "That is not a move this record can make."
    http_status = 422


class ReasonRequired(StaffError):
    error_code = "REASON_REQUIRED"
    default_message = "Say why, so the record explains itself later."
    http_status = 422


class LastWorkingDayRequired(StaffError):
    error_code = "LAST_WORKING_DAY_REQUIRED"
    default_message = "Give the last working day."
    http_status = 422


class AccountNotEligible(StaffError):
    """The identity layer refuses the account action for that state."""

    error_code = "ACCOUNT_NOT_ELIGIBLE"
    default_message = "This account is not in a state where that can be done."
    http_status = 422


class InvalidDateRange(StaffError):
    """Same code ``vs_calendar`` uses for the same shape of mistake."""

    error_code = "INVALID_DATE_RANGE"
    default_message = "The end date cannot be before the start date."
    http_status = 422


class SessionArchived(StaffError):
    error_code = "SESSION_ARCHIVED"
    default_message = (
        "That school year has been archived, so its teaching cannot be changed. "
        "Its existing assignments are still readable."
    )
    http_status = 422


class BranchNotInService(StaffError):
    error_code = "BRANCH_NOT_IN_SERVICE"
    default_message = "That branch is not in service, so nobody can be posted to it."
    http_status = 422


class StaffHasLeft(StaffError):
    """Moving the posting of somebody who no longer works here.

    The roster shows them, because hiding them would lose that they were ever
    at the branch, and it draws them as finished and will not tick them. This
    is the same rule at the door: a screen that greys a row is a courtesy, and
    the refusal is what makes it true.
    """

    error_code = "STAFF_HAS_LEFT"
    default_message = (
        "They no longer work here, so their posting cannot be moved. Reinstate "
        "them first if they are coming back."
    )
    http_status = 422


class LeadAlreadySet(StaffError):
    """Promoting somebody where the pairing already has a lead.

    Refused rather than silently replacing, so a school displaces a colleague
    deliberately instead of by accident.
    """

    error_code = "LEAD_ALREADY_SET"
    default_message = "This class already has a lead teacher for that subject."
    http_status = 422


class LeaveAlreadyDecided(StaffError):
    """A correction aimed at a request that has been approved or rejected.

    A request whose dates change after approval is a different request, and
    editing it in place would leave an approval attached to something nobody
    approved.
    """

    error_code = "LEAVE_ALREADY_DECIDED"
    default_message = (
        "This leave request has already been decided, so it cannot be changed. "
        "Cancel it and file a new one."
    )
    http_status = 422


class FieldNotSelfEditable(StaffError):
    """A person editing something about themselves that is the school's to set.

    422 with the fields named, rather than a bare 403. A 403 tells a form the
    whole request was refused and leaves it with nowhere to put the message; the
    reader has to be shown which box to stop changing, and that is a field
    error's job.
    """

    error_code = "FIELD_NOT_SELF_EDITABLE"
    default_message = (
        "You cannot change this about yourself. Ask a school administrator."
    )
    http_status = 422


class CannotActOnSelf(StaffError):
    """Ending your own employment, or closing your own login, from this module.

    Both are one click from a school locking itself out, and the smaller the
    school the likelier it is: at a school with one administrator there is
    nobody left to undo it, and Terminated deactivates the account for good.
    Somebody genuinely leaving is recorded by a colleague, which is also who
    would have to do it if they had already gone.
    """

    error_code = "CANNOT_ACT_ON_SELF"
    default_message = "You cannot do that to your own record."
    http_status = 422


class InvitationAlreadyAccepted(StaffError):
    """A revoke aimed at an account that is past PENDING.

    Worded as the invitation having been accepted rather than as a missing row,
    because the row is there and the caller is entitled to know why the action
    does not apply.
    """

    error_code = "INVITATION_ALREADY_ACCEPTED"
    default_message = (
        "This invitation has already been accepted, so there is nothing to "
        "revoke. Suspend the account instead."
    )
    http_status = 422
