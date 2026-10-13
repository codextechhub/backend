"""Who is in a class on a given day.

**A class's roll is read as at a date**, never as the class stands today, so a
roll asked for last Tuesday lists the children who were in the class last
Tuesday:

* a child belongs to the class from the placement's ``effective_date``, which
  is the day the school says the placement takes effect, not the day somebody
  recorded it;
* a placement is superseded by the child's next placement (the one with the
  later ``effective_date``), so a move agreed on Friday to start on Monday
  leaves the child in the old class until Monday;
* a child who had left the roll by that day (withdrawn, transferred out or
  graduated, as the status log dates it) is not listed.

**A suspended child stays on the roll, flagged.** Status is read as at the
date from the status log, so a child suspended on Monday and back on Thursday is
flagged Monday to Wednesday and not on Thursday, whether or not the automatic
return has run. What a flagged child may do is for the caller to decide: a
register locks the row and keeps it out of every count, while a gradebook or
an elective list keeps the child on it.

Several modules read this roll (attendance registers, the gradebook's score
sheets, the elective takers of a class), which is why it lives with the
students it is made of.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from schools.vs_students.constants import LEAVES_THE_ROLL, StudentStatus
from schools.vs_students.models import ClassEnrolment, StudentStatusLog


@dataclass(frozen=True)
class RosterEntry:
    student: object
    suspended: bool


def placement_in_force(rows, on_date):
    """The placement a child was in on *on_date*, or None.

    *rows* are all of one child's placements in the class's school year. The
    one in force is the last to have taken effect by that day.
    """
    taken = [row for row in rows if row.effective_date <= on_date]
    if not taken:
        return None
    return max(taken, key=lambda row: (row.effective_date, row.assigned_at, row.pk))


def status_as_at(tenant, student_ids, on_date: dt.date) -> dict:
    """``{student_id: status}`` for each child with a status change by *on_date*.

    The status is the destination of the latest change that had taken effect by
    that day. A suspension that set a return date reads as the status it
    returned the child to once that date has come, so a child is back on the
    day the school said even before the automatic return has been recorded. A
    child with no status change on record is absent from the answer.
    """
    if not student_ids:
        return {}
    latest = (
        StudentStatusLog.objects.filter(
            tenant=tenant, student_id__in=student_ids, effective_date__lte=on_date,
        )
        .order_by("student_id", "-effective_date", "-changed_at", "-id")
        .distinct("student_id")
    )
    return {log.student_id: status_after(log, on_date) for log in latest}


def status_after(log, on_date: dt.date) -> str:
    """The status *log* leaves a child in on *on_date*, a later return date applied.

    A suspension that set a return date reads as the status it returned the
    child to once that date has come.
    """
    status = log.to_status
    if status == StudentStatus.SUSPENDED and log.return_date and log.return_date <= on_date:
        status = log.from_status or StudentStatus.ACTIVE
    return status


def status_on(logs, on_date: dt.date):
    """One child's status on *on_date* from their own status *logs*, or None.

    *logs* are every status change of the one child, in any order. The status is
    the destination of the latest change that had taken effect by that day, as
    :func:`status_as_at` reads it for many children at once, so a term's worth
    of days is answered from one query rather than one per day.
    """
    taken = [log for log in logs if log.effective_date <= on_date]
    if not taken:
        return None
    latest = max(taken, key=lambda log: (log.effective_date, log.changed_at, log.pk))
    return status_after(latest, on_date)


def suspended_student_ids(tenant, student_ids, on_date: dt.date) -> set:
    """The children among *student_ids* who were suspended on *on_date*."""
    return {
        student_id
        for student_id, status in status_as_at(tenant, student_ids, on_date).items()
        if status == StudentStatus.SUSPENDED
    }


def roster_as_at(tenant, school_class, on_date: dt.date) -> list[RosterEntry]:
    """The children on *school_class*'s register on *on_date*, by name."""
    candidate_ids = set(
        ClassEnrolment.objects.filter(
            tenant=tenant, school_class=school_class, effective_date__lte=on_date,
        ).values_list("student_id", flat=True),
    )
    if not candidate_ids:
        return []

    by_student: dict[int, list] = {}
    for row in ClassEnrolment.objects.filter(
        tenant=tenant, session=school_class.session, student_id__in=candidate_ids,
    ).select_related("student"):
        by_student.setdefault(row.student_id, []).append(row)

    students = []
    for rows in by_student.values():
        current = placement_in_force(rows, on_date)
        if current is not None and current.school_class_id == school_class.pk:
            students.append(current.student)

    statuses = status_as_at(tenant, [s.pk for s in students], on_date)
    students = [s for s in students if statuses.get(s.pk) not in LEAVES_THE_ROLL]
    students.sort(key=lambda s: (s.last_name.lower(), s.first_name.lower(), s.pk))
    return [
        RosterEntry(student=s, suspended=statuses.get(s.pk) == StudentStatus.SUSPENDED)
        for s in students
    ]


def rosters_as_at(tenant, school_classes, on_date: dt.date) -> dict:
    """``{class_id: [RosterEntry]}`` for every class in *school_classes* on *on_date*.

    The same roster :func:`roster_as_at` reads for one class, worked out for
    many in a fixed number of queries however many classes there are: the
    placements of the children who could be in any of them, those children's
    placements in each class's school year, and their status log. Every class
    named has an entry, empty when nobody is placed in it.
    """
    classes = list(school_classes)
    out = {school_class.pk: [] for school_class in classes}
    if not classes:
        return out

    candidate_ids = set(
        ClassEnrolment.objects.filter(
            tenant=tenant, school_class__in=classes, effective_date__lte=on_date,
        ).values_list("student_id", flat=True),
    )
    if not candidate_ids:
        return out

    by_student: dict[tuple, list] = {}
    for row in ClassEnrolment.objects.filter(
        tenant=tenant, session_id__in={c.session_id for c in classes},
        student_id__in=candidate_ids,
    ).select_related("student"):
        by_student.setdefault((row.session_id, row.student_id), []).append(row)

    placed: dict[int, list] = {school_class.pk: [] for school_class in classes}
    for (session_id, _student_id), rows in by_student.items():
        current = placement_in_force(rows, on_date)
        if current is not None and current.school_class_id in placed:
            placed[current.school_class_id].append(current.student)

    statuses = status_as_at(
        tenant, {s.pk for students in placed.values() for s in students}, on_date,
    )
    for class_id, students in placed.items():
        students = [s for s in students if statuses.get(s.pk) not in LEAVES_THE_ROLL]
        students.sort(key=lambda s: (s.last_name.lower(), s.first_name.lower(), s.pk))
        out[class_id] = [
            RosterEntry(student=s, suspended=statuses.get(s.pk) == StudentStatus.SUSPENDED)
            for s in students
        ]
    return out
