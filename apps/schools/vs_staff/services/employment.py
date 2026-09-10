"""Moving a person through the employment lifecycle.

This is the one place ``StaffProfile.employment_status`` is written, and the one
place this module asks the identity layer to change an account. The two facts
stay apart: employment says whether somebody still works here, the account says
whether the login may be used today, and each transition states in words what it
does to the other.

**Nothing here writes ``User.status``.** Each account effect calls the same
service the platform's own endpoint calls, so the eligibility rules, the auth
event and the session blacklisting are asked once and answered once. A second
copy of "what suspending means" would drift from the first the week somebody
added a status.

The status change, the event row and the account call are one transaction. A
person suspended on their record and still able to sign in is the failure that
prevents.

FRD M12 v2.1, FR-008 and section 5.2.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..constants import (
    ACCOUNT_EFFECT,
    EMPLOYMENT_TRANSITIONS,
    LAST_WORKING_DAY_REQUIRED_FOR,
    REASON_REQUIRED_FOR,
    UNACCEPTED_ACCOUNT_STATUSES,
    EmploymentStatus,
)
from ..exceptions import (
    AccountNotEligible,
    CannotActOnSelf,
    InvalidStatusTransition,
    LastWorkingDayRequired,
    ReasonRequired,
)
from . import audit
from .scoping import is_self

#: What the confirmation says before each move, in the words the screen shows.
#:
#: RESIGNED says an administrator closes the account rather than that it closes
#: itself. Nothing in this repository runs on a schedule, so a sentence
#: promising that the login stops working on the last working day would be a
#: promise the server does not keep; FRD section 3.4 records the refusal and
#: FR-002's directory warning is what a school gets instead.
ACCOUNT_EFFECT_TEXT = {
    EmploymentStatus.ACTIVE: "They will be able to sign in again.",
    EmploymentStatus.ON_LEAVE: "This does not change whether they can sign in.",
    EmploymentStatus.SUSPENDED: (
        "They will not be able to sign in. Their record and their assignments "
        "are kept."
    ),
    EmploymentStatus.RESIGNED: (
        "Their account stays usable after their last working day. Close it "
        "yourself when they have finished handing over."
    ),
    EmploymentStatus.TERMINATED: (
        "They will not be able to sign in, and their account cannot be reopened "
        "without CodeX. Their record and their history are kept."
    ),
}


#: Said when there are no moves for a reason the reader would otherwise guess at.
#:
#: An empty list has two meanings and only one of them is about the record: an
#: invited person has nowhere to go until they accept, and a closed record stays
#: closed. Being refused because it is your own record is neither, and a screen
#: that fell back to the general sentence would tell an administrator her own
#: record was closed.
NO_MOVES_ON_YOUR_OWN_RECORD = (
    "You cannot change your own employment status. Ask another administrator "
    "to do it."
)


def transitions_note(staff, actor=None) -> str | None:
    """Why there is nothing to choose from, where the reason is not the record."""
    return NO_MOVES_ON_YOUR_OWN_RECORD if is_self(actor, staff) else None


def allowed_transitions(staff, actor=None) -> tuple[str, ...]:
    """Where this person can go next, for the drawer's Move-to list.

    Empty when the reader is the person: :func:`change_status` refuses those
    moves, and a list that offered them would be a drawer contradicting the API
    it posts to.
    """
    if is_self(actor, staff):
        return ()
    return EMPLOYMENT_TRANSITIONS.get(staff.employment_status, ())


def assignments_needing_cover(staff):
    """The classes this person teaches, named rather than counted.

    "3 classes need cover" sends a head teacher hunting through a roster of a
    hundred and nine; "JSS1 A Mathematics, JSS1 B Mathematics, SSS2 Physics"
    does not. The platform already applies this rule when it refuses a switch to
    per-branch payroll, and it is worth keeping.

    Only the active session's assignments count. Last year's are the record of
    who taught what and need no cover.
    """
    from schools.vs_academics.models import AcademicSession, SessionStatus

    active = AcademicSession.all_objects.filter(
        tenant_id=staff.tenant_id, status=SessionStatus.ACTIVE,
    ).values_list("pk", flat=True).first()
    if active is None:
        return []
    rows = (
        staff.teaching_assignments.filter(session_id=active)
        .select_related("subject", "school_class")
        .order_by("school_class__name", "subject__name")
    )
    return [f"{row.school_class.name} {row.subject.name}" for row in rows]


def _apply_account_effect(staff, to_status, actor, request):
    """Ask the identity layer to do what section 5.2 says this transition does.

    Its refusal is translated rather than swallowed: an account already
    deactivated, or one suspended from a state suspension does not apply to, is
    a state the caller is entitled to be told about.
    """
    from vs_user.services.user import UserStatusService

    effect = ACCOUNT_EFFECT.get(to_status)
    if effect is None:
        return
    method = getattr(UserStatusService, effect)
    try:
        method(staff.user, actor, request=request)
    except ValueError as error:
        payload = error.args[0] if error.args else {}
        message = (
            payload.get("message") if isinstance(payload, dict) else str(payload)
        )
        raise AccountNotEligible(message or AccountNotEligible.default_message)


@transaction.atomic
def change_status(staff, *, to_status, actor, effective_date=None, reason="",
                  last_working_day=None, note="", request=None):
    """Move somebody, log it, and do to their account exactly what that means.

    Returns ``(staff, event)``. The caller renders the confirmation from
    :data:`ACCOUNT_EFFECT_TEXT` and the cover list from
    :func:`assignments_needing_cover`; neither is computed here, because a
    service that formats a sentence is a service two screens later disagree with.
    """
    from ..models import StaffEmploymentEvent

    # Nobody ends their own employment here. Every move this endpoint offers
    # from Active either closes the login or ends the job, and the school's own
    # administrator is now on the list like everybody else.
    if is_self(actor, staff):
        raise CannotActOnSelf(
            "You cannot change your own employment status. Ask another "
            "administrator to do it.",
        )

    from_status = staff.employment_status
    if to_status not in EMPLOYMENT_TRANSITIONS.get(from_status, ()):
        raise InvalidStatusTransition(
            f"Somebody who is {_label(from_status)} cannot be moved to "
            f"{_label(to_status)}.",
            from_status=from_status, to_status=to_status,
        )
    if to_status in REASON_REQUIRED_FOR and not (reason or "").strip():
        raise ReasonRequired(
            f"Say why this person is being marked {_label(to_status).lower()}.",
            field="reason",
        )
    if to_status in LAST_WORKING_DAY_REQUIRED_FOR and last_working_day is None:
        raise LastWorkingDayRequired(
            "Give the last working day, so the school knows when cover starts.",
            field="last_working_day",
        )

    effective_date = effective_date or timezone.localdate()

    staff.employment_status = to_status
    fields = ["employment_status", "updated_at"]
    if to_status in LAST_WORKING_DAY_REQUIRED_FOR:
        staff.exit_date = last_working_day
        fields.append("exit_date")
    staff.save(update_fields=fields)

    event = StaffEmploymentEvent.objects.create(
        tenant=staff.tenant, staff=staff, from_status=from_status,
        to_status=to_status, reason=reason or "", effective_date=effective_date,
        last_working_day=last_working_day, note=note or "", changed_by=actor,
    )
    _apply_account_effect(staff, to_status, actor, request)
    audit.emit_employment_status_changed(staff, event, actor=actor)
    return staff, event


def starting_status(user) -> str:
    """Where a new record's history starts, read off the account.

    Invited is right for somebody who has just been sent a link and wrong for
    somebody already signing in. The promotion out of Invited happens once, when
    the invited person uses their link, so a record opened at Invited for an
    account that activated last term stays Invited for the rest of that person's
    employment: the head teacher given a second posting in March would read as
    an unaccepted invitation while teaching every day.
    """
    return (
        EmploymentStatus.INVITED
        if getattr(user, "status", "") in UNACCEPTED_ACCOUNT_STATUSES
        else EmploymentStatus.ACTIVE
    )


@transaction.atomic
def record_creation(staff, *, actor, effective_date=None):
    """The first event on a record, written when the person is invited.

    ``from_status`` is blank, which is what says "this is where the history
    starts" rather than "we do not know what they were before".
    """
    from ..models import StaffEmploymentEvent

    return StaffEmploymentEvent.objects.create(
        tenant=staff.tenant, staff=staff, from_status="",
        to_status=staff.employment_status,
        effective_date=effective_date or timezone.localdate(),
        reason="Added to the staff list", changed_by=actor,
    )


@transaction.atomic
def promote_on_activation(user):
    """INVITED to ACTIVE, when the invited person uses their own link.

    The only place in this module where an account event drives employment, and
    it is deliberate. There is no second act of an administrator declaring
    somebody employed: inventing one would leave a school with people who had
    accepted their invitation and still read as Invited, and an administrator
    doing it *instead* of the person would move the employment status while the
    account stayed PENDING.

    A user with no staff profile activates exactly as before and this does
    nothing. A school's first administrator, provisioned before this module
    existed, is that case, and so is any account the platform creates for
    itself.
    """
    from ..models import StaffEmploymentEvent, StaffProfile

    staff = StaffProfile.all_objects.filter(user=user).first()
    if staff is None or staff.employment_status != EmploymentStatus.INVITED:
        return None

    staff.employment_status = EmploymentStatus.ACTIVE
    staff.save(update_fields=["employment_status", "updated_at"])
    event = StaffEmploymentEvent.objects.create(
        tenant=staff.tenant, staff=staff,
        from_status=EmploymentStatus.INVITED, to_status=EmploymentStatus.ACTIVE,
        effective_date=timezone.localdate(), reason="Accepted invitation",
        changed_by=None,
    )
    audit.emit_employment_status_changed(staff, event, actor=None)
    return event


def _label(status: str) -> str:
    return dict(EmploymentStatus.choices).get(status, status)
