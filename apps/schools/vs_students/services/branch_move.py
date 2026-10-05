"""Moving a pupil to another branch of the school, fee account and all.

Tunde attends Ikeja and his family moves to Lekki. From 25 January he attends
Lekki: his record names Lekki, he sits in a Lekki class, and Lekki's bursar
chases what he owes. One act does all three, in one transaction, so there is
never a day on which the roll says Lekki and the books say Ikeja.

**The finance half goes through the FAL.** The move asks
:meth:`~schools.core.fal.ports.StudentCustomerPort.move_account` to re-file the
pupil's fee accounts at Lekki. The engine then hands Lekki the open bills, the
unspent credit and the income not yet earned on the move date; the income Ikeja
already earned stays in Ikeja's books, and Lekki owes Ikeja for it through the
inter-branch account. A refusal there (a closed month at either branch) is a
refusal of the whole move: the pupil stays at Ikeja.

**Who may move a pupil.** The ``school.students.change_branch`` key, and a reach
that covers both branches (:func:`vs_rbac.scoping.caller_may_change` over the
pair). The move writes into both branches: the pupil leaves Ikeja's roll and
debt book and joins Lekki's, so an Ikeja-only administrator may not push a
pupil and their debt onto Lekki, and a Lekki-only one may not pull one away
from Ikeja. That is the rule an inter-branch transfer's void already keeps
("somebody who works in both branches"). A whole-school administrator covers
every pair.

**Each move is a row** (:class:`~schools.vs_students.models.StudentBranchMove`),
and the row's id is the reference the finance move is keyed by, so a retry of
one move cannot move the balance twice. A move is not undone: moving the pupil
back is a second move, which moves whatever is then open back with them and
leaves both in the history. The finance move alone can still be voided from
Finance while nothing it moved has been paid, credited or released at the new
branch.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event
from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_rbac.scoping import caller_may_change

from ..constants import ON_ROLL, TransferReason
from ..exceptions import (
    AlreadyAtBranch,
    BranchNotOpen,
    FinanceDidNotAnswer,
    MoveClassRequired,
    MoveDateRefused,
    NoActiveSession,
    NotOnRoll,
    OneBranchSchool,
)
from ..models import Student, StudentBranchMove
from .scoping import assert_class_reachable, branch_dimension_applies

#: A branch a pupil may move into. Any other status is closed or not yet open.
OPEN_BRANCH = "ACTIVE"


@dataclass(frozen=True)
class BranchMoveResult:
    """A move made, or previewed.

    ``accounts`` holds one :class:`~schools.core.fal.contracts.AccountMove` per
    fee account of the pupil, with what it carried; it is empty for a pupil
    who has never been billed. ``move`` is ``None`` on a preview.
    """

    from_branch: object
    to_branch: object
    effective_date: object
    accounts: tuple
    move: StudentBranchMove | None = None
    school_class: object = None
    over_capacity: bool = False


def move_targets(student, user):
    """The branches *user* may move *student* to, in name order.

    Every open branch of the school other than the pupil's own whose pair with
    it the caller's reach covers. Empty at a school with one branch, and empty
    for a caller who works only at the pupil's branch.
    """
    from vs_rbac.scoping import visible_branch_ids
    from vs_tenants.models import Branch

    branches = (
        Branch.all_objects.filter(tenant=student.tenant, status=OPEN_BRANCH)
        .exclude(pk=student.branch_id).order_by("name")
    )
    visible = visible_branch_ids(user, student.tenant)
    return [
        branch for branch in branches
        if caller_may_change(
            user, student.tenant, (student.branch_id, branch.pk), visible=visible,
        )
    ]


def current_placement(student):
    """``(year, placement)``: the running year and the pupil's place in it, each ``None`` where absent."""
    from .placement import active_session

    try:
        session = active_session(student.tenant)
    except NoActiveSession:
        return None, None
    current = (
        student.enrolments.filter(session=session, is_active=True)
        .select_related("school_class", "school_class__branch").first()
    )
    return session, current


def check_move(student, to_branch, *, user, effective_date=None):
    """Refuse a move that cannot be made, or return the day it takes effect.

    Checked in the order a person would ask: is there anywhere to go, is the
    pupil on the roll, is it somewhere else, is it open, may this caller move
    between the two, and is the day a real one. The day defaults to today at
    the new branch, and may be earlier (a move recorded a day late) but not
    later than today there, nor before the pupil's current class placement
    began.
    """
    tenant = student.tenant
    if not branch_dimension_applies(tenant):
        raise OneBranchSchool()
    if student.status not in ON_ROLL:
        raise NotOnRoll(
            f"{student.first_name} is not on the roll, so there is no branch to "
            f"move them from.",
            status=student.status,
        )
    if to_branch.pk == student.branch_id:
        raise AlreadyAtBranch(
            f"{student.first_name} already attends {to_branch.name}.",
        )
    if to_branch.status != OPEN_BRANCH:
        raise BranchNotOpen(
            f"{to_branch.name} is not open, so no pupil can move into it.",
        )
    if not caller_may_change(user, tenant, (student.branch_id, to_branch.pk)):
        raise PermissionDenied(
            f"Moving {student.first_name} from {student.branch.name} to "
            f"{to_branch.name} needs somebody who works at both branches.",
        )

    today = branch_today(tenant, to_branch.pk)
    when = effective_date or today
    if when > today:
        raise MoveDateRefused(
            f"A move is recorded on or after the day it happens. Today at "
            f"{to_branch.name} is {format_date(today, tenant)}.",
            effective_date=str(when), today=str(today),
        )
    _, current = current_placement(student)
    if current is not None and when < current.effective_date:
        raise MoveDateRefused(
            f"{student.first_name} joined {current.school_class.name} on "
            f"{format_date(current.effective_date, tenant)}, so the move cannot "
            f"be dated before that.",
            effective_date=str(when), placed_on=str(current.effective_date),
        )
    return when


def _check_class(student, to_branch, school_class_id, *, user):
    """The class the pupil joins at the new branch, or ``None`` to keep their own.

    A pupil in a class of their old branch needs a class at the new one. A
    pupil in a school-wide class may keep it, since it belongs to every branch,
    and an unplaced pupil may stay unplaced, as they were. A class named must
    be one the caller can see (404 otherwise, as everywhere), this year's, and
    at the new branch or school-wide. It is resolved after the move itself is
    checked, so a caller refused the move is told why rather than that a class
    of the other branch does not exist.
    """
    from .placement import active_session, assert_class_is_in_session, resolve_class

    _, current = current_placement(student)
    school_class = (
        resolve_class(student.tenant, user, school_class_id) if school_class_id else None
    )
    if school_class is None:
        if current is not None and current.school_class.branch_id is not None:
            raise MoveClassRequired(
                f"{student.first_name} is in {current.school_class.name} at "
                f"{student.branch.name}. Choose the class they join at "
                f"{to_branch.name}.",
            )
        return None
    assert_class_is_in_session(school_class, active_session(student.tenant))
    assert_class_reachable(to_branch, school_class)
    return school_class


def _finance(student, from_branch, to_branch, *, move_ref, when, actor, reason, dry_run):
    """Ask the FAL to move the pupil's fee accounts; refuse the move if it cannot answer."""
    from schools.core.fal import get_student_customer

    result = get_student_customer().move_account(
        student.pk, from_branch_ref=from_branch.pk, to_branch_ref=to_branch.pk,
        move_ref=move_ref, move_date=when, actor_ref=getattr(actor, "pk", None),
        reason=reason, dry_run=dry_run,
    )
    if not result.is_available:
        raise FinanceDidNotAnswer(reason=result.reason)
    return result.unwrap()


def preview_move(student, to_branch, *, user, effective_date=None):
    """What moving *student* to *to_branch* would carry, with nothing moved.

    The same checks as the move, then the FAL's own move run and discarded, so
    the figures are the ones the move books and a refusal the move would meet
    (a closed month at either branch) is met here.
    """
    when = check_move(student, to_branch, user=user, effective_date=effective_date)
    accounts = _finance(
        student, student.branch, to_branch, move_ref="preview", when=when,
        actor=user, reason="", dry_run=True,
    )
    return BranchMoveResult(
        from_branch=student.branch, to_branch=to_branch, effective_date=when,
        accounts=accounts,
    )


@transaction.atomic
def move_to_branch(student, to_branch, *, actor, reason, effective_date=None,
                   school_class_id=None, allow_over_capacity=False):
    """Move *student* to *to_branch*, with their class and fee account, or refuse.

    Everything happens in this one transaction, in this order: the pupil row
    is locked and checked again, the move is recorded, the pupil's branch
    changes, they are placed in their new class (the class move's own rules
    apply, capacity included), and the FAL moves their fee accounts under the
    move's id. Any refusal along the way, the finance one included, leaves the
    pupil where they were.
    """
    from .placement import place

    locked = Student.all_objects.select_for_update().select_related("branch", "tenant").get(
        pk=student.pk,
    )
    when = check_move(locked, to_branch, user=actor, effective_date=effective_date)
    school_class = _check_class(locked, to_branch, school_class_id, user=actor)
    from_branch = locked.branch
    _, current = current_placement(locked)

    move = StudentBranchMove.objects.create(
        tenant=locked.tenant, student=locked, from_branch=from_branch,
        to_branch=to_branch, effective_date=when, reason=reason,
        moved_by=actor,
    )
    locked.branch = to_branch
    locked.save(update_fields=["branch", "updated_at"])

    over = False
    if school_class is not None:
        enrolment, _, over = place(
            locked, school_class, actor=actor, reason=TransferReason.BRANCH_MOVE,
            effective_date=when, allow_over_capacity=allow_over_capacity,
        )
        if current is None or enrolment.pk != current.pk:
            move.from_enrolment = current
            move.to_enrolment = enrolment
            move.save(update_fields=["from_enrolment", "to_enrolment", "updated_at"])

    accounts = _finance(
        locked, from_branch, to_branch, move_ref=f"M{move.pk}", when=when,
        actor=actor, reason=reason, dry_run=False,
    )

    joined = f", into {school_class.name}" if school_class is not None else ""
    summary = (
        f"{locked.full_name} moved from {from_branch.name} to {to_branch.name} "
        f"on {format_date(when, locked.tenant)}{joined}."
    )
    if accounts:
        summary += " Their fee account moved with them."
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_BRANCH_CHANGED,
        entity_type="Student", entity_id=str(locked.pk),
        entity_label=locked.full_name, tenant=locked.tenant, actor_user=actor,
        summary=summary,
        metadata={
            "move_id": move.pk, "from_branch": from_branch.pk,
            "to_branch": to_branch.pk, "effective_date": str(when),
            "school_class": getattr(school_class, "pk", None),
            "fee_accounts_moved": len(accounts),
        },
    )
    return BranchMoveResult(
        from_branch=from_branch, to_branch=to_branch, effective_date=when,
        accounts=accounts, move=move,
        school_class=school_class or getattr(current, "school_class", None),
        over_capacity=over,
    )
