"""Fee holds: keeping a student's result from release until the school says so.

**A hold is a person's decision.** No result is ever held automatically: not for
a balance, not for its age, not for anything. The bursar places a hold with a
reason, and lifts it with an optional note.

**The list** is the roll of every active class in the caller's branches for the
term, by class name and then student name, with the balance and the oldest unpaid
date Finance reports for the students on the page and the time they were read.
It never carries a score, a grade or an average: a bursar who works holds has no
reason to see how a child did, and a hold placed with a mark in front of the
person placing it is a hold that could be said to be about the mark.

**Finance is read through the FAL** (:func:`schools.core.fal.registry.get_finance_reader`),
once for the whole page, filtered to its students. A student the debtors list
does not name owes nothing. When Finance cannot answer, the list says so
(``finance_available`` false) and every balance is null, never zero: a zero would
read as "paid up".

**Placing** needs a reason (``REASON_REQUIRED``), a student on the roll of a class
the caller can see (404 otherwise), and a result that has not been released
(``ALREADY_RELEASED``) or already held (``ALREADY_HELD``). **Lifting** records who
and when. If the student's class is already published the result is released
then, and the guardian is told, exactly as publication does it.

The class's term is locked while a hold is placed or lifted, in the same order
publication locks it, so a hold placed while a class is being published is either
seen by the publication or refused because the result has been released.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from schools.core.fal import registry
from schools.core.fal.contracts import FilterClause
from schools.core.fal.exceptions import EntityNotProvisioned
from schools.vs_academics.models import SchoolClass
from schools.vs_attendance.services.scope import placed_classes, school_branches
from schools.vs_students.models import ClassEnrolment, Student
from schools.vs_students.services.roster import roster_as_at
from schools.vs_students.services.scoping import scope_classes
from vs_audit.models import AuditActionType
from vs_config.clock import branch_today, branch_zone
from vs_rbac.scoping import only_branch_id_or_several, visible_branch_ids

from ..exceptions import AlreadyHeld, AlreadyReleased, HoldNotOpen, ReasonRequired
from ..models import ClassTerm, ResultHold, StudentTermResult
from . import audit, book, hubs, publishing

#: The most debtor rows one call to Finance asks for; the FAL caps a page at 100.
FINANCE_PAGE = 100


@dataclass(frozen=True)
class Seat:
    """A student on a class's roll for the term: who, in which class, at which branch."""

    student: object
    school_class: object
    home: object


@dataclass(frozen=True)
class Finance:
    """What Finance said about a page of students, and when.

    ``owed`` maps a student's id to ``(outstanding kobo, oldest unpaid day)``; a
    student it does not name owes nothing. ``available`` is False when Finance
    could not answer.
    """

    available: bool
    as_of: object
    owed: dict


def seats(tenant, user, term, branch, class_id=None) -> list:
    """Every roll student of the term's classes the caller can see, by class and student name."""
    placed, _, _, _ = hubs.reachable_classes(tenant, user, term, branch)
    if class_id is not None:
        placed = [pair for pair in placed if pair[0].pk == class_id]
        if not placed:
            raise NotFound("No such class at this school.")
    placed.sort(key=lambda pair: (pair[0].name.lower(), pair[0].pk))
    rolls = book.rolls_for(tenant, term, placed)
    return [
        Seat(entry.student, school_class, home)
        for school_class, home in placed
        for entry in rolls.get(school_class.pk, [])
    ]


def locate(tenant, user, term, student_id) -> Seat:
    """The seat of *student_id* on a visible class's roll for *term*, else a 404."""
    student = Student.objects.filter(tenant=tenant, pk=student_id).first()
    if student is None:
        raise NotFound("No such student on a class roll this term.")
    class_ids = ClassEnrolment.objects.filter(
        tenant=tenant, student=student, session_id=term.session_id,
    ).values_list("school_class_id", flat=True)
    classes = list(
        scope_classes(
            SchoolClass.objects.filter(
                tenant=tenant, pk__in=list(class_ids), is_active=True,
                session_id=term.session_id,
            ),
            user, tenant,
        ).select_related(*hubs.CLASS_RELATIONS)
    )
    placed, _ = placed_classes(
        classes, school_branches(tenant), visible_branch_ids(user, tenant),
    )
    for school_class, home in placed:
        day = min(branch_today(tenant, home), term.end_date)
        if any(entry.student.pk == student.pk for entry in roster_as_at(tenant, school_class, day)):
            return Seat(student, school_class, home)
    raise NotFound("No such student on a class roll this term.")


# ── Finance ────────────────────────────────────────────────────────────────

def read_finance(tenant, students) -> Finance:
    """Balance and oldest unpaid day for *students* from Finance, or an unavailable answer."""
    school = getattr(tenant, "school_profile", None)
    refs = [str(student.pk) for student in students]
    if school is None:
        return Finance(False, timezone.now(), {})
    if not refs:
        return Finance(True, timezone.now(), {})
    reader = registry.get_finance_reader()
    wanted = set(refs)
    owed: dict = {}
    as_of = None
    page = 1
    try:
        while True:
            result = reader.debtors(
                school.pk, None, (FilterClause("student_ref", "in", refs),),
                page=page, page_size=FINANCE_PAGE,
            )
            as_of = result.computed_at
            if not result.is_available:
                return Finance(False, as_of, {})
            for row in result.value.items:
                if row.student_ref not in wanted:
                    continue
                total, oldest = owed.get(row.student_ref, (0, None))
                days = [d for d in (oldest, row.oldest_unpaid_on) if d is not None]
                owed[row.student_ref] = (total + row.outstanding, min(days) if days else None)
            if page >= result.value.total_pages:
                break
            page += 1
    except EntityNotProvisioned:
        return Finance(False, timezone.now(), {})
    return Finance(True, as_of, {int(ref): value for ref, value in owed.items()})


# ── Rows ───────────────────────────────────────────────────────────────────

def _released_on(released_at, tenant, home):
    return released_at.astimezone(branch_zone(tenant, home)).date()


def rows_payload(tenant, term, page, *, finance: Finance | None = None) -> dict:
    """``{"rows": [...], "as_of": time, "finance_available": bool}`` for a page of seats."""
    students = [seat.student for seat in page]
    ids = [student.pk for student in students]
    finance = finance or read_finance(tenant, students)
    holds = {
        hold.student_id: hold
        for hold in ResultHold.objects.filter(
            tenant=tenant, term=term, lifted_at__isnull=True, student_id__in=ids,
        ).select_related("placed_by")
    }
    released = dict(
        StudentTermResult.objects.filter(
            tenant=tenant, class_term__term=term, student_id__in=ids,
            released_at__isnull=False,
        ).values_list("student_id", "released_at")
    )
    several = only_branch_id_or_several(tenant) is None
    rows = []
    for seat in page:
        student, home = seat.student, seat.home
        hold = holds.get(student.pk)
        balance = oldest = days = None
        if finance.available:
            balance, oldest = finance.owed.get(student.pk, (0, None))
            if oldest is not None:
                days = max(0, (branch_today(tenant, home) - oldest).days)
        rows.append({
            "student": {
                "id": student.pk, "name": student.full_name,
                "admission_number": student.student_number,
            },
            "class": {"id": seat.school_class.pk, "name": seat.school_class.name},
            "branch": {"id": home.pk, "name": home.name} if several else None,
            "balance": balance, "oldest_unpaid_on": oldest, "days_owed": days,
            "hold": (
                {
                    "id": hold.pk, "reason": hold.reason,
                    "placed_by": hold.placed_by.full_name if hold.placed_by else None,
                    "placed_at": hold.placed_at,
                }
                if hold is not None else None
            ),
            "released": student.pk in released,
            "released_on": (
                _released_on(released[student.pk], tenant, home)
                if student.pk in released else None
            ),
        })
    return {"rows": rows, "as_of": finance.as_of, "finance_available": finance.available}


# ── Placing and lifting ────────────────────────────────────────────────────

def _lock_class_term(tenant, seat: Seat, term):
    return (
        ClassTerm.objects.select_for_update()
        .filter(tenant=tenant, school_class=seat.school_class, term=term).first()
    )


def place(tenant, user, term, student_id, reason) -> Seat:
    """Hold *student_id*'s result for *term*; returns the student's seat."""
    reason = (reason or "").strip()
    if not reason:
        raise ReasonRequired("Say why this result is being held.", field="reason")
    seat = locate(tenant, user, term, student_id)
    with transaction.atomic():
        _lock_class_term(tenant, seat, term)
        if StudentTermResult.objects.filter(
            tenant=tenant, student=seat.student, class_term__term=term,
            released_at__isnull=False,
        ).exists():
            raise AlreadyReleased()
        if ResultHold.objects.filter(
            tenant=tenant, term=term, student=seat.student, lifted_at__isnull=True,
        ).exists():
            raise AlreadyHeld()
        try:
            with transaction.atomic():
                hold = ResultHold.objects.create(
                    tenant=tenant, term=term, student=seat.student, reason=reason,
                    placed_by=user,
                )
        except IntegrityError:
            raise AlreadyHeld() from None
        audit.record(
            tenant=tenant, user=user, branch=seat.home,
            entity_type="ResultHold", entity_id=hold.pk,
            entity_label=f"{seat.student.full_name}, {term.name}",
            summary=f"{user.full_name} held {seat.student.full_name}'s result for {term.name}.",
            action_type=AuditActionType.CREATE,
            student_id=seat.student.pk, term_id=term.pk, class_id=seat.school_class.pk,
        )
    return seat


def lift(tenant, user, hold_id, note):
    """Lift a hold; returns ``(hold, seat, released)``.

    *released* is True when the student's class was already published and the
    result was released by lifting.
    """
    found = ResultHold.objects.select_related("term", "term__session").filter(
        tenant=tenant, pk=hold_id,
    ).first()
    if found is None:
        raise NotFound("No such hold at this school.")
    seat = locate(tenant, user, found.term, found.student_id)
    now = timezone.now()
    with transaction.atomic():
        _lock_class_term(tenant, seat, found.term)
        hold = ResultHold.objects.select_for_update().get(pk=found.pk)
        if hold.lifted_at is not None:
            raise HoldNotOpen()
        hold.lifted_by, hold.lifted_at, hold.lift_note = user, now, (note or "").strip()
        hold.save(update_fields=["lifted_by", "lifted_at", "lift_note", "updated_at"])
        released = publishing.release_after_lift(tenant, seat.student, found.term, now)
        audit.record(
            tenant=tenant, user=user, branch=seat.home,
            entity_type="ResultHold", entity_id=hold.pk,
            entity_label=f"{seat.student.full_name}, {found.term.name}",
            summary=(
                f"{user.full_name} lifted the hold on {seat.student.full_name}'s result "
                f"for {found.term.name}{' and released it' if released else ''}."
            ),
            action_type=AuditActionType.UPDATE,
            student_id=seat.student.pk, term_id=found.term.pk,
            class_id=seat.school_class.pk, released=released,
        )
    return hold, seat, released



def hold_payload(hold) -> dict:
    """A hold as the lift answers with it, before Field Access is applied."""
    return {
        "id": hold.pk, "reason": hold.reason,
        "placed_by": hold.placed_by.full_name if hold.placed_by else None,
        "placed_at": hold.placed_at,
        "lifted_by": hold.lifted_by.full_name if hold.lifted_by else None,
        "lifted_at": hold.lifted_at, "lift_note": hold.lift_note,
    }
