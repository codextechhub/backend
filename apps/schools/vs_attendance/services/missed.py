"""Missed a lesson: in school in the morning, absent from a lesson.

**The rule.** A student *missed a lesson* when the class register of the day is
submitted and marks them Present or Late, and the lesson register is submitted
and marks them Absent. Both registers must be submitted: a draft is not yet a
statement of where a child was. A student absent in the morning has not missed
a lesson, they were not in school, and one marked Excused in the morning was
away with a reason.

**Neither mark overwrites the other, and a missed lesson is never an absence.**
Nothing here writes to a class register, and no rate in the module reads a
lesson mark: the attendance rate, the hub's figures, the student's day cells and
the chronic list are drawn from class registers only. A missed lesson is its
own fact, shown beside the rate and never inside it.

**Where it is read.** The administrator's hub (one day, as a count and a list),
a student's Attendance tab (a term, as the lessons they were absent from) and
the report (a term, per subject, class and teacher). All three ask the database
the same question: each lesson Absent mark of a submitted lesson register, with
the student's mark in the class register of the same class and day as a
subquery. The question is a fixed number of queries however many lessons there
are.

A mark is trusted as it was taken. A pupil suspended after the day, with a
suspension dated back over it, still has the mark they were given.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Lower

from ..constants import Mark
from ..models import LessonMark, LessonRegister, StudentMark

#: The morning marks that put a child in school.
IN_SCHOOL = (Mark.PRESENT, Mark.LATE)


def lesson_absences(tenant, *, start: dt.date, end: dt.date, class_ids=None, student=None):
    """Every Absent lesson mark of a submitted lesson register between two dates.

    Each row carries ``morning``: the student's mark in the submitted class
    register of the same class and day, or None when there is none. Narrowed to
    *class_ids* or to one *student* when given.
    """
    morning = StudentMark.objects.filter(
        register__school_class_id=OuterRef("register__school_class_id"),
        register__date=OuterRef("register__date"),
        register__submitted_at__isnull=False,
        student_id=OuterRef("student_id"),
    ).values("mark")[:1]
    marks = LessonMark.objects.filter(
        tenant=tenant, mark=Mark.ABSENT, register__submitted_at__isnull=False,
        register__date__range=(start, end),
    )
    if class_ids is not None:
        marks = marks.filter(register__school_class_id__in=list(class_ids))
    if student is not None:
        marks = marks.filter(student=student)
    return marks.annotate(morning=Subquery(morning))


def missed(tenant, *, start: dt.date, end: dt.date, class_ids):
    """The Absent lesson marks of students who were in school that morning."""
    return lesson_absences(tenant, start=start, end=end, class_ids=class_ids).filter(
        morning__in=IN_SCHOOL,
    )


def missed_on(tenant, on_date: dt.date, class_ids) -> list:
    """The day's missed lessons, by class, start time then student name.

    Each element is a :class:`~..models.LessonMark` with ``morning`` set and its
    student, register and register's subject loaded.
    """
    rows = list(
        missed(tenant, start=on_date, end=on_date, class_ids=class_ids)
        .select_related("student", "register", "register__subject", "register__school_class"),
    )
    rows.sort(key=lambda row: (
        row.register.school_class.name.lower(), row.register.starts_at,
        row.student.last_name.lower(), row.student.first_name.lower(), row.student_id,
    ))
    return rows


def lessons_missed_rows(tenant, *, start: dt.date, end: dt.date, class_ids, marking_codes) -> list:
    """One row per subject, class and teacher with a submitted lesson register in the range.

    ``missed`` counts the missed-a-lesson events and ``students`` names the
    distinct students who had one, so a row with no event reads 0 and an empty
    list. Only classes whose level code is in *marking_codes* (lower-cased) are
    counted, so a level switched off since the lesson was taken drops out of
    the table with everything else about lessons.
    """
    registers = (
        LessonRegister.objects.filter(
            tenant=tenant, school_class_id__in=list(class_ids),
            date__range=(start, end), submitted_at__isnull=False,
        )
        .annotate(level_code=Lower("school_class__level__code"))
        .filter(level_code__in=list(marking_codes))
        .values(
            "subject_id", "subject__name", "school_class_id", "school_class__name",
            "teacher_id", "teacher__first_name", "teacher__last_name",
        )
        .annotate(lessons=Count("pk"))
    )
    rows = {}
    for entry in registers:
        key = (entry["subject_id"], entry["school_class_id"], entry["teacher_id"])
        teacher = f"{entry['teacher__first_name'] or ''} {entry['teacher__last_name'] or ''}".strip()
        rows[key] = {
            "subject": entry["subject__name"],
            "class_name": entry["school_class__name"],
            "teacher_name": teacher or None,
            "missed": 0,
            "students": [],
        }
    if not rows:
        return []

    names = defaultdict(set)
    events = (
        missed(tenant, start=start, end=end, class_ids=class_ids)
        .values(
            "register__subject_id", "register__school_class_id", "register__teacher_id",
            "student_id", "student__first_name", "student__last_name",
        )
        .annotate(times=Count("pk"))
    )
    for entry in events:
        key = (
            entry["register__subject_id"], entry["register__school_class_id"],
            entry["register__teacher_id"],
        )
        row = rows.get(key)
        if row is None:
            continue
        row["missed"] += entry["times"]
        names[key].add(f"{entry['student__first_name']} {entry['student__last_name']}".strip())
    for key, row in rows.items():
        row["students"] = sorted(names.get(key, ()), key=str.lower)
    return sorted(
        rows.values(),
        key=lambda row: (row["class_name"].lower(), row["subject"].lower(), row["teacher_name"] or ""),
    )
