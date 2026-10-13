"""A class's term worked out across its subjects: the broadsheet, and the figures behind it.

One class in one term, read once (:func:`load_book`). The broadsheet, a
student's compile drawer, the class submission and the publication all read
through it, so a total, an average and a position are the same number on every
screen and on the report card.

**What is in.** Only a sheet that is in (:func:`.book.is_in`) contributes a
figure; a sheet that is not in leaves its cells "not in yet" and its subject in
the list of gaps. A subject an optional-subject student does not take is
"not taken" for them and counts for nothing.

**Overall average** is the mean of the unrounded percentages of the subjects the
student takes whose sheet is in; a subject excused entirely is left out. It is
shown to two decimal places and ranked at two decimal places, so ties share a
position and the next is skipped (:func:`.compute.positions`).

**Position is a school's choice.** When ``gradebook.rank_in_class`` is off no
position is computed and none is sent anywhere.

**The roll** is the class roll as at the earlier of the class's branch's today
and the term's last day, in name order, and it includes a suspended student
(:func:`.book.rolls_for`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import AcademicSession, AcademicTerm
from schools.vs_attendance.services.term_summary import class_term_summary

from ..constants import (
    CFG_CONDUCT_RATINGS,
    CFG_RANK_IN_CLASS,
    CFG_RATING_KEY,
    CFG_RATING_TRAITS,
    CFG_SHOW_CLASS_STATS,
    CellState,
)
from ..exceptions import NoScale
from ..models import AssessmentStructure, StudentTermResult
from . import access, book, compute, figures, resolve
from .rules import GradebookRules, read_gradebook_rules
from .sheets import LOCKED_STAGES

#: The settings a class's figures and its report card read.
BOOK_RULES = (
    CFG_RANK_IN_CLASS, CFG_CONDUCT_RATINGS, CFG_RATING_TRAITS, CFG_SHOW_CLASS_STATS,
    CFG_RATING_KEY,
)

#: How a caller reaches a class: the broadsheet is read by the class teacher or
#: anyone overseeing results; a compile is read by the class teacher or the
#: approver and written by the class teacher alone; the approver's actions
#: (return, comments, publication) and the release list and report cards are
#: guarded by their key alone.
READ_BOOK, READ_COMPILE, COMPILE, APPROVER, RELEASE = (
    "read_book", "read_compile", "compile", "approver", "release",
)


@dataclass
class ClassContext:
    """A class the caller may open, in a term of its own school year."""

    tenant: object
    user: object
    school_class: object
    branch: object
    term: object

    @property
    def class_term(self):
        return resolve.class_term(self.tenant, self.school_class, self.term)


def open_class(tenant, user, class_id, term, *, rule) -> ClassContext:
    """The class *class_id* for *term*, for a caller the *rule* lets in.

    A class of another school, or one in a branch the caller cannot see, answers
    404 before anything about the caller's right to it is said, so identifiers
    cannot be enumerated. A term from another school year than the class's is
    refused (:class:`~..exceptions.InvalidTerm`).
    """
    school_class, branch = access.open_class(tenant, user, class_id)
    access.assert_term_of_class(school_class, term)
    if rule == READ_BOOK:
        access.assert_may_read_class(user, tenant, school_class)
    elif rule == READ_COMPILE:
        access.assert_may_read_compile(user, tenant, school_class)
    elif rule == COMPILE:
        access.assert_class_teacher(user, school_class)
    return ClassContext(tenant, user, school_class, branch, term)


@dataclass
class Column:
    """One subject of the class: who takes it, who teaches it and, once it is in, every line."""

    item: book.DueSheet
    takers: frozenset
    lines: dict = field(default_factory=dict)

    @property
    def subject(self):
        return self.item.subject

    @property
    def is_in(self) -> bool:
        return self.item.is_in

    def takes(self, student_id) -> bool:
        return student_id in self.takers


@dataclass
class ClassBook:
    """Everything the class-level screens and the publication need, worked out once."""

    tenant: object
    school_class: object
    branch: object
    term: object
    rules: GradebookRules
    entry: book.ClassEntry
    roll: list
    columns: list
    components: list
    bands: list
    structure: AssessmentStructure | None
    results: dict
    averages: dict
    positions: dict

    @property
    def class_term(self):
        return self.entry.class_term

    @property
    def stage(self) -> str:
        return self.entry.stage

    @property
    def students(self) -> list:
        return [roll_entry.student for roll_entry in self.roll]

    @property
    def missing(self) -> list:
        """The columns whose sheet is not in yet."""
        return [column for column in self.columns if not column.is_in]

    def comment_of(self, student_id) -> str:
        result = self.results.get(student_id)
        return (result.teacher_comment or "").strip() if result is not None else ""

    @property
    def comments_missing(self) -> int:
        return sum(1 for student in self.students if not self.comment_of(student.pk))


def _structure(class_term, tenant, school_class):
    """The structure the class's term was fixed to, else None (no mark has been entered)."""
    if class_term is not None and class_term.structure_id is not None:
        return AssessmentStructure.objects.prefetch_related("components").get(
            pk=class_term.structure_id,
        )
    return None


def _bands(tenant, school_class) -> list:
    try:
        return list(resolve.scale_for(tenant, school_class).bands.all())
    except NoScale:
        return []


def load_book(tenant, school_class, branch, term, *, rules=None) -> ClassBook:
    """The class's term read and worked out. Reads only; nothing is created."""
    rules = rules or read_gradebook_rules(tenant, keys=BOOK_RULES)
    entry = book.load(tenant, term, [(school_class, branch)])[0]
    structure = _structure(entry.class_term, tenant, school_class)
    components = list(structure.components.all()) if structure is not None else []
    bands = _bands(tenant, school_class)

    in_sheets = [item.sheet.pk for item in entry.due if item.is_in]
    scores = figures.stored_scores(tenant, in_sheets)
    by_id = {roll_entry.student.pk: roll_entry.student for roll_entry in entry.roll}

    columns = []
    for item in entry.due:
        column = Column(item=item, takers=frozenset(item.roll_ids))
        if item.is_in:
            students = [by_id[pk] for pk in item.roll_ids if pk in by_id]
            column.lines = {
                line.student.pk: line
                for line in figures.lines(components, bands, students, scores[item.sheet.pk])
            }
        columns.append(column)

    averages = {}
    for student_id in entry.roll_ids:
        averages[student_id] = compute.overall_average(
            column.lines[student_id].result.percentage
            for column in columns
            if column.is_in and student_id in column.lines
        )
    positions = compute.positions(averages) if rules.rank_in_class else {}

    results = (
        {
            row.student_id: row
            for row in StudentTermResult.objects.filter(
                tenant=tenant, class_term=entry.class_term,
            )
        }
        if entry.class_term is not None else {}
    )
    return ClassBook(
        tenant=tenant, school_class=school_class, branch=branch, term=term, rules=rules,
        entry=entry, roll=list(entry.roll), columns=columns, components=components,
        bands=bands, structure=structure, results=results, averages=averages,
        positions=positions,
    )


# ── The broadsheet ─────────────────────────────────────────────────────────

def _person(staff) -> dict | None:
    return {"id": staff.pk, "name": staff.user.full_name} if staff is not None else None


def _returned(class_term) -> dict | None:
    if class_term is None or not class_term.returned_note:
        return None
    by = class_term.returned_by
    return {
        "by": by.full_name if by is not None else None,
        "at": class_term.returned_at, "note": class_term.returned_note,
    }


def days_not_taken(bk: ClassBook) -> int:
    """The school days so far this term on which the class owed a register and none was submitted."""
    return class_term_summary(bk.tenant, bk.school_class, bk.term).days_not_taken


def cell_for(column: Column, student_id) -> dict:
    """The broadsheet cell for one student in one column."""
    subject_id = column.subject.pk
    if not column.takes(student_id):
        return {"subject": subject_id, "state": CellState.NOT_TAKEN, "total": None}
    line = column.lines.get(student_id) if column.is_in else None
    if line is None or line.result.total is None:
        return {"subject": subject_id, "state": CellState.NOT_IN, "total": None}
    return {"subject": subject_id, "state": CellState.TOTAL, "total": line.result.total}


def broadsheet_payload(bk: ClassBook, user) -> dict:
    """The broadsheet for the class, before Field Access is applied."""
    school_class = bk.school_class
    ranks = bk.rules.rank_in_class
    rows = []
    for student in bk.students:
        row = {
            "student": {
                "id": student.pk, "name": student.full_name,
                "admission_number": student.student_number,
            },
            "cells": [cell_for(column, student.pk) for column in bk.columns],
            "average": compute.round2(bk.averages.get(student.pk)),
            "comment_done": bool(bk.comment_of(student.pk)),
        }
        if ranks:
            row["position"] = bk.positions.get(student.pk)
            row["position_of"] = len(bk.roll)
        rows.append(row)

    missing = bk.missing
    comments_missing = bk.comments_missing
    stage = bk.stage
    is_open = stage not in LOCKED_STAGES
    warning = days_not_taken(bk)
    return {
        "class": {"id": school_class.pk, "name": school_class.name},
        "term": {"id": bk.term.pk, "name": bk.term.name},
        "class_teacher": _person(school_class.class_teacher),
        "stage": stage,
        "returned": _returned(bk.class_term),
        "subjects": [
            {
                "id": column.subject.pk, "name": column.subject.name,
                "short": column.subject.code, "in": column.is_in,
                "main_teacher": _person(column.item.teacher),
            }
            for column in bk.columns
        ],
        "rows": rows,
        "gaps": [
            {
                "subject": {"id": column.subject.pk, "name": column.subject.name},
                "main_teacher": _person(column.item.teacher),
            }
            for column in missing
        ],
        "attendance_warning": {"days_not_taken": warning} if warning else None,
        "can_submit": (
            is_open and access.is_class_teacher(user, school_class)
            and bool(bk.columns) and not missing and comments_missing == 0
        ),
        "submit_blockers": {
            "subjects_missing": [
                {"id": column.subject.pk, "name": column.subject.name} for column in missing
            ],
            "comments_missing": comments_missing,
        },
        "ranks": ranks,
    }


# ── The compile drawer ─────────────────────────────────────────────────────

def student_in_roll(bk: ClassBook, student_id) -> int:
    """The 0-based place of *student_id* on the class roll, else a 404."""
    for index, student in enumerate(bk.students):
        if student.pk == student_id:
            return index
    raise NotFound("No such student on this class's roll.")


def compile_payload(bk: ClassBook, student_id, user) -> dict:
    """One student's compile drawer, before Field Access is applied."""
    index = student_in_roll(bk, student_id)
    students = bk.students
    student = students[index]

    subjects = []
    for column in bk.columns:
        if not column.takes(student_id):
            continue
        cell = cell_for(column, student_id)
        line = column.lines.get(student_id) if cell["state"] == CellState.TOTAL else None
        subjects.append({
            "subject": {"id": column.subject.pk, "name": column.subject.name},
            "total": cell["total"],
            "grade": line.result.grade if line is not None else None,
            "state": cell["state"],
        })

    counts = class_term_summary(bk.tenant, bk.school_class, bk.term).students.get(student_id)
    result = bk.results.get(student_id)
    locked = bk.stage in LOCKED_STAGES
    payload = {
        "student": {
            "id": student.pk, "name": student.full_name,
            "admission_number": student.student_number,
        },
        "index": index + 1, "of": len(students),
        "previous": students[index - 1].pk if index > 0 else None,
        "next": students[index + 1].pk if index + 1 < len(students) else None,
        "subjects": subjects,
        "average": compute.round2(bk.averages.get(student_id)),
        "attendance": {
            "opened": counts.opened if counts else 0,
            "present": counts.present if counts else 0,
            "absent": counts.absent if counts else 0,
        },
        "teacher_comment": result.teacher_comment if result is not None else "",
        "stage": bk.stage,
        "can_edit": (not locked) and access.is_class_teacher(user, bk.school_class),
    }
    if bk.rules.conduct_ratings:
        payload["ratings"] = dict(result.ratings) if result is not None else {}
        payload["traits"] = list(bk.rules.rating_traits)
    return payload


def percentage_of(value: Decimal | None) -> str | None:
    """*value* as the two-place string the screens and the snapshot print."""
    rounded = compute.round2(value)
    return None if rounded is None else str(rounded)


def next_term_start(tenant, term):
    """The day the term after *term* begins, else None.

    The next term of the same school year by its order, else the first term of
    the next school year by start date. None when the school has neither, which a
    card prints as nothing.
    """
    following = (
        AcademicTerm.objects.filter(
            tenant=tenant, session_id=term.session_id, order_index__gt=term.order_index,
        ).order_by("order_index").first()
    )
    if following is None:
        next_year = (
            AcademicSession.objects.filter(
                tenant=tenant, start_date__gt=term.session.start_date,
            ).order_by("start_date").first()
        )
        if next_year is not None:
            following = AcademicTerm.objects.filter(
                tenant=tenant, session=next_year,
            ).order_by("order_index").first()
    return following.start_date if following is not None else None
