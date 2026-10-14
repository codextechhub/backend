"""The promotion line of a cumulative report card, read live from Student Management.

The gradebook never decides promotion. When the end-of-year run (or a manual
graduation) has recorded what became of a pupil's placement in the card's school
year, the card says so; when nothing is recorded it says nothing.

**The record read** is ``ClassEnrolment.outcome`` on the pupil's placement in
that school year, which Student Management writes at the moment of the decision:
the promotion run sets ``PROMOTED`` or ``REPEATED`` on the placement it ends and
places the pupil in the next year, and a graduation (the run's, or a status
change to graduated) sets ``GRADUATED``. That per-pupil row is the authority.
``StudentPromotionBatch`` is only the run's counts and links to no pupil, so it
cannot say what happened to one. A placement still ``CURRENT``, or ended by a
transfer, a withdrawal or an ordinary move of class, records no outcome, and a
pupil the run left where they were (``HOLD``) is unchanged, so none of those
produces a line.

**The class named** is where the pupil was placed in the next school year: their
earliest placement in a year that starts after the card's year, which is the one
the run wrote. A pupil promoted but with no such placement found is "Promoted"
alone.

**The words**: ``Promoted to <class>``, ``To repeat <class>``, ``Graduated``.

Student Management declares no Field Access switch for a placement's outcome or
for a pupil's class history, so there is nothing of theirs to apply; the
gradebook's own ``promotion_line`` switch governs the line on the card and the
Results tab.
"""
from __future__ import annotations

from schools.vs_academics.models import AcademicSession
from schools.vs_students.constants import EnrolmentOutcome
from schools.vs_students.models import ClassEnrolment

GRADUATED_LINE = "Graduated"

_RECORDED = (
    EnrolmentOutcome.PROMOTED, EnrolmentOutcome.REPEATED, EnrolmentOutcome.GRADUATED,
)


def promotion_lines(tenant, session_id, student_ids) -> dict:
    """``{student_id: line}`` for the students whose placement in *session_id* has an outcome.

    A student with nothing recorded is absent. Two queries however many students
    are asked about (a third only when somebody was promoted or told to repeat).
    """
    student_ids = list(student_ids)
    if not student_ids:
        return {}
    outcomes = {}
    for row in (
        ClassEnrolment.objects.filter(
            tenant=tenant, session_id=session_id, student_id__in=student_ids,
            outcome__in=_RECORDED,
        ).order_by("ended_at", "pk")
    ):
        outcomes[row.student_id] = row.outcome
    if not outcomes:
        return {}

    moving = [pk for pk, outcome in outcomes.items() if outcome != EnrolmentOutcome.GRADUATED]
    next_class = _next_classes(tenant, session_id, moving) if moving else {}

    lines = {}
    for student_id, outcome in outcomes.items():
        if outcome == EnrolmentOutcome.GRADUATED:
            lines[student_id] = GRADUATED_LINE
            continue
        name = next_class.get(student_id)
        if outcome == EnrolmentOutcome.PROMOTED:
            lines[student_id] = f"Promoted to {name}" if name else "Promoted"
        elif name:
            lines[student_id] = f"To repeat {name}"
        else:
            lines[student_id] = "To repeat"
    return lines


def _next_classes(tenant, session_id, student_ids) -> dict:
    """``{student_id: class name}`` of each student's earliest placement after *session_id*'s year."""
    start = AcademicSession.objects.filter(tenant=tenant, pk=session_id).values_list(
        "start_date", flat=True,
    ).first()
    if start is None:
        return {}
    found = {}
    for row in (
        ClassEnrolment.objects.filter(
            tenant=tenant, student_id__in=student_ids, session__start_date__gt=start,
        )
        .select_related("school_class")
        .order_by("session__start_date", "assigned_at", "pk")
    ):
        found.setdefault(row.student_id, row.school_class.name)
    return found
