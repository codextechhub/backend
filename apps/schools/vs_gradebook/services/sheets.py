"""The score sheet: reading it, saving scores to it and submitting it.

A **sheet** is one subject for one class for one term. This module is the whole
of what the main teacher does on it.

**Reading** builds the header (who teaches it, the examination paper, where it
stands, what the caller may do), the components of the class's structure and a
line for every student on the sheet's roll: their cells, and what is worked out
from them (:mod:`.figures`). It reads a sheet that has not been started as
NOT_STARTED; nothing is created by a read.

**Saving** is a batch of cells, all or nothing. Every cell is checked before any
is written, and a refusal names the cell, so the screen keeps what the teacher
typed and marks the ones to fix. A blank cell (no mark, no marker) deletes the
stored value, because a blank is never a zero. The class's term and the sheet
are created by the first mark, and the first mark is also where the class's
structure is fixed, so that every sheet of one class shares its columns.
Nothing about a save is audited: it is autosave, and the trail records the acts
that change who may do what next (submit, return, approve).

**Locks.** A sheet that has been submitted or approved takes no more scores, and
neither does any sheet of a class that is awaiting the principal or published.
The class lock is asked first because it is the reason when both apply.

**Submitting** needs every cell filled (``ABS`` and ``EXC`` count). The sheet
then goes to the head of its department when the class's programme is one the
school moderates and the department has a head, and otherwise straight to the
class teacher; the answer says which.

**Moderated or not, before it is submitted.** ``needs_moderation`` on an
unsubmitted sheet is the answer the rule gives now, so the screen can say where
the sheet will go; once submitted it is the answer that was given then.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from schools.vs_students.services.roster import roster_as_at
from vs_audit.models import AuditActionType
from vs_config.clock import branch_today

from ..constants import (
    CELL_BAD_MARKER,
    CELL_DUPLICATE,
    CELL_MARK_AND_MARKER,
    CELL_NOT_ON_SHEET,
    CELL_UNKNOWN_COMPONENT,
    CFG_MODERATED_PROGRAMS,
    NEXT_CLASS_TEACHER,
    NEXT_MODERATION,
    Marker,
    SheetStatus,
    StoredStage,
)
from ..exceptions import (
    BlanksRemain,
    CellsRefused,
    ClassLocked,
    NegativeMark,
    NotAWholeMark,
    OverMax,
    SheetLocked,
    StructureChanged,
)
from ..models import AssessmentStructure, ClassTerm, Score, ScoreSheet
from . import access, audit, book, compute, figures, resolve
from .rules import read_gradebook_rules

LOCKED_STATUSES = (SheetStatus.SUBMITTED, SheetStatus.APPROVED)
LOCKED_STAGES = (StoredStage.AWAITING, StoredStage.PUBLISHED)
MARKERS = {Marker.ABSENT.value, Marker.EXCUSED.value}


@dataclass
class SheetContext:
    """Everything one sheet's read, save and submit need, loaded once."""

    tenant: object
    user: object
    school_class: object
    branch: object
    term: object
    subject: object
    is_elective: bool
    class_term: ClassTerm | None
    sheet: ScoreSheet | None
    structure: AssessmentStructure
    components: list
    bands: list
    roll: list
    class_roll_size: int

    @property
    def students(self) -> list:
        return [entry.student for entry in self.roll]

    @property
    def status(self) -> str:
        return self.sheet.status if self.sheet is not None else SheetStatus.NOT_STARTED


def _structure_of(class_term, tenant, school_class) -> AssessmentStructure:
    """The structure fixed on the class's term, else the one in force now."""
    if class_term is not None and class_term.structure_id is not None:
        return AssessmentStructure.objects.prefetch_related("components").get(
            pk=class_term.structure_id,
        )
    return resolve.structure_for(tenant, school_class)


READ, ENTER, ANY = "read", "enter", None


def load(tenant, user, class_id, subject_id, term, *, rule=READ) -> SheetContext:
    """The sheet of *subject_id* in class *class_id* for *term*, for a caller who may open it.

    Answers 404 for a class or subject that is not this school's, outside the
    caller's branches, not offered at the class's level or optional and taken by
    nobody. The caller's right to the sheet is asked next, as *rule* says:
    ``READ`` (a part in the sheet, or a hub reader), ``ENTER`` (its main teacher
    or an ``enter_any`` holder) or ``ANY`` for an action whose key is the whole
    of the rule, such as a reminder. Only then are the structure and scale looked
    up, so a stranger learns nothing of how the class is set up.
    """
    school_class, branch = access.open_class(tenant, user, class_id)
    access.assert_term_of_class(school_class, term)
    subject, is_elective, takers = access.open_subject(tenant, school_class, term, subject_id)
    if rule == ENTER:
        access.assert_may_enter(user, tenant, school_class, subject)
    elif rule == READ:
        access.assert_may_read(user, tenant, school_class, subject)

    class_term = resolve.class_term(tenant, school_class, term)
    sheet = (
        ScoreSheet.objects.filter(tenant=tenant, class_term=class_term, subject=subject)
        .select_related("returned_by", "moderated_by").first()
        if class_term is not None else None
    )
    structure = _structure_of(class_term, tenant, school_class)
    scale = resolve.scale_for(tenant, school_class)

    on_date = min(branch_today(tenant, branch), term.end_date)
    full_roll = roster_as_at(tenant, school_class, on_date)
    roll = (
        [entry for entry in full_roll if entry.student.pk in takers]
        if is_elective else full_roll
    )
    return SheetContext(
        tenant=tenant, user=user, school_class=school_class, branch=branch, term=term,
        subject=subject, is_elective=is_elective, class_term=class_term, sheet=sheet,
        structure=structure, components=list(structure.components.all()),
        bands=list(scale.bands.all()), roll=roll, class_roll_size=len(full_roll),
    )


# ── Where the sheet stands ─────────────────────────────────────────────────

def lock_error(ctx: SheetContext):
    """The refusal that applies to a write on this sheet now, or None."""
    if ctx.class_term is not None and ctx.class_term.stage in LOCKED_STAGES:
        return ClassLocked()
    if ctx.sheet is not None and ctx.sheet.status in LOCKED_STATUSES:
        return SheetLocked()
    return None


def routes_to_moderation(ctx: SheetContext) -> bool:
    """Whether a submitted sheet goes to the department head before it counts.

    The class's programme is one the school moderates, and the subject's
    department has a head who is still on the staff roll.
    """
    rules = read_gradebook_rules(ctx.tenant, keys=(CFG_MODERATED_PROGRAMS,))
    return (
        rules.moderates(ctx.school_class.level.program)
        and resolve.head_of_subject(ctx.subject) is not None
    )


def exam_paper(ctx: SheetContext) -> dict | None:
    """The published examination paper for the class and subject inside the term, or None."""
    found = book.exam_papers(ctx.tenant, ctx.term, [ctx.school_class.pk], [ctx.subject.pk])
    return found.get((ctx.school_class.pk, ctx.subject.pk))


def _person(staff) -> dict | None:
    if staff is None:
        return None
    return {"id": staff.pk, "name": staff.user.full_name}


def class_figures(lines) -> dict:
    """The class figures block of a sheet from its lines."""
    found = compute.sheet_figures(line.result for line in lines)
    return {
        "class_average": compute.round2(found.average),
        "highest": found.highest, "lowest": found.lowest, "pass_rate": found.pass_rate,
    }


def row_payload(line: figures.Line, components) -> dict:
    result = line.result
    return {
        "student": {
            "id": line.student.pk, "name": line.student.full_name,
            "admission_number": line.student.student_number,
        },
        "cells": [
            {"component": component.pk, "mark": mark, "marker": marker}
            for component, (mark, marker) in zip(components, line.cells)
        ],
        "total": result.total, "obtainable": result.obtainable,
        "percentage": compute.round2(result.percentage),
        "grade": result.grade, "remark": result.remark, "is_fail": result.is_fail,
        "complete": result.complete, "excused_note": figures.excused_note(result),
    }


def payload(ctx: SheetContext) -> dict:
    """The sheet as the screen reads it, before Field Access is applied."""
    scores = (
        figures.stored_scores(ctx.tenant, [ctx.sheet.pk]).get(ctx.sheet.pk, {})
        if ctx.sheet is not None else {}
    )
    lines = figures.lines(ctx.components, ctx.bands, ctx.students, scores)
    blanks = sum(line.blank_cells for line in lines)
    complete = sum(1 for line in lines if line.result.complete)

    lock = lock_error(ctx)
    can_enter = lock is None and access.may_enter(
        ctx.user, ctx.tenant, ctx.school_class, ctx.subject,
    )
    sheet = ctx.sheet
    submitted = sheet is not None and sheet.status in LOCKED_STATUSES
    returned = None
    if sheet is not None and sheet.returned_at is not None:
        returned = {
            "by": sheet.returned_by.full_name if sheet.returned_by else None,
            "at": sheet.returned_at, "note": sheet.returned_note,
        }
    teacher = resolve.main_teacher(ctx.school_class, ctx.subject, ctx.school_class.session_id)
    return {
        "subject": {"id": ctx.subject.pk, "name": ctx.subject.name},
        "class": {"id": ctx.school_class.pk, "name": ctx.school_class.name},
        "term": {"id": ctx.term.pk, "name": ctx.term.name},
        "main_teacher": _person(teacher),
        "assistants": [
            staff.user.full_name
            for staff in resolve.assistants(ctx.school_class, ctx.subject, ctx.school_class.session_id)
        ],
        "exam_paper": exam_paper(ctx),
        "status": ctx.status,
        "needs_moderation": sheet.needs_moderation if submitted else routes_to_moderation(ctx),
        "is_elective": ctx.is_elective,
        "takers": len(ctx.roll) if ctx.is_elective else None,
        "roll": ctx.class_roll_size if ctx.is_elective else None,
        "returned": returned,
        "saved_at": sheet.saved_at if sheet is not None else None,
        "progress": {"complete": complete, "total": len(lines)},
        "can_enter": can_enter,
        "can_submit": can_enter and blanks == 0,
        "blank_count": blanks,
        "components": [
            {
                "id": c.pk, "name": c.name, "short_name": c.short_name,
                "max_score": c.max_score, "is_exam": c.is_exam,
            }
            for c in ctx.components
        ],
        "rows": [row_payload(line, ctx.components) for line in lines],
        "stats": class_figures(lines),
    }


# ── Creating the rows a first write needs ──────────────────────────────────

def ensure_sheet(ctx: SheetContext, *, fix_structure: bool):
    """``(class_term, sheet)`` for the context, creating what is missing.

    With ``fix_structure`` the class's term takes the structure the sheet was
    read with, which is how the first mark fixes it for every sheet of the class.
    Called inside the caller's transaction. A class whose columns were fixed to a
    different structure since the sheet was read is refused
    (:class:`~..exceptions.StructureChanged`), so scores never rest on columns the
    rest of the class does not use.
    """
    class_term = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term, create=True)
    if fix_structure:
        if class_term.structure_id is None:
            class_term.structure = ctx.structure
            class_term.save(update_fields=["structure", "updated_at"])
        elif class_term.structure_id != ctx.structure.pk:
            raise StructureChanged()
    sheet, _ = ScoreSheet.objects.get_or_create(
        tenant=ctx.tenant, class_term=class_term, subject=ctx.subject,
    )
    return class_term, sheet


def _lock(class_term, sheet) -> ScoreSheet:
    """The sheet re-read under a row lock, refusing if it was locked since it was loaded."""
    locked = ScoreSheet.objects.select_for_update().get(pk=sheet.pk)
    if ClassTerm.objects.get(pk=class_term.pk).stage in LOCKED_STAGES:
        raise ClassLocked()
    if locked.status in LOCKED_STATUSES:
        raise SheetLocked()
    return locked


# ── Saving scores ──────────────────────────────────────────────────────────

def _whole_mark(value):
    """*value* as a whole number, or None when it is not one (a decimal, a string, a flag)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def check_cells(ctx: SheetContext, cells) -> list:
    """``[(student_id, component_id, mark, marker)]`` for the batch, or raise :class:`CellsRefused`.

    Nothing is written here. Every cell is judged, so one refusal lists all of
    the batch's problems at once.
    """
    roll_ids = {student.pk for student in ctx.students}
    components = {component.pk: component for component in ctx.components}
    clean, errors, seen = [], [], set()

    for cell in cells:
        student_id, component_id = cell["student"], cell["component"]
        mark, marker = cell.get("mark"), cell.get("marker") or None

        def refuse(code, message):
            errors.append({
                "student": student_id, "component": component_id,
                "code": code, "message": message,
            })

        if (student_id, component_id) in seen:
            refuse(CELL_DUPLICATE, "This cell appears twice in the same save.")
            continue
        seen.add((student_id, component_id))
        if student_id not in roll_ids:
            refuse(CELL_NOT_ON_SHEET, "This student is not on this sheet.")
            continue
        component = components.get(component_id)
        if component is None:
            refuse(
                CELL_UNKNOWN_COMPONENT,
                "This column is not part of this class's assessment structure.",
            )
            continue
        if marker is not None and marker not in MARKERS:
            refuse(CELL_BAD_MARKER, "Use ABS for absent or EXC for excused.")
            continue
        if mark is not None and marker is not None:
            refuse(CELL_MARK_AND_MARKER, "Give a mark, or ABS, or Excused, not more than one.")
            continue
        whole = None
        if mark is not None:
            whole = _whole_mark(mark)
            if whole is None:
                refuse(NotAWholeMark.error_code, NotAWholeMark.default_message)
                continue
            if whole < 0:
                refuse(NegativeMark.error_code, NegativeMark.default_message)
                continue
            if whole > component.max_score:
                refuse(OverMax.error_code, f"Max {component.max_score}")
                continue
        clean.append((student_id, component_id, whole, marker))

    if errors:
        raise CellsRefused(cells=errors)
    return clean


def save_scores(ctx: SheetContext, cells) -> None:
    """Write a batch of cells to the sheet, or refuse the whole batch.

    The caller has already judged who may enter. Locks are asked here, before
    the cells and again under the sheet's row lock, so a submit that lands
    between the two cannot be written over.
    """
    error = lock_error(ctx)
    if error is not None:
        raise error
    clean = check_cells(ctx, cells)
    marks = [cell for cell in clean if cell[2] is not None or cell[3] is not None]
    if ctx.sheet is None and not marks:
        return

    now = timezone.now()
    with transaction.atomic():
        class_term, sheet = ensure_sheet(ctx, fix_structure=bool(marks))
        sheet = _lock(class_term, sheet)

        existing = {
            (row.student_id, row.component_id): row
            for row in Score.objects.filter(tenant=ctx.tenant, sheet=sheet)
        }
        create, update, delete = [], [], []
        for student_id, component_id, mark, marker in clean:
            row = existing.get((student_id, component_id))
            if mark is None and marker is None:
                if row is not None:
                    delete.append(row.pk)
                continue
            if row is None:
                create.append(Score(
                    tenant=ctx.tenant, sheet=sheet, student_id=student_id,
                    component_id=component_id, mark=mark, marker=marker,
                    entered_by=ctx.user, entered_at=now,
                ))
            elif (row.mark, row.marker) != (mark, marker):
                row.mark, row.marker = mark, marker
                row.entered_by, row.entered_at, row.updated_at = ctx.user, now, now
                update.append(row)

        if not (create or update or delete):
            return
        if create:
            Score.objects.bulk_create(create)
        if update:
            Score.objects.bulk_update(
                update, ["mark", "marker", "entered_by", "entered_at", "updated_at"],
            )
        if delete:
            Score.objects.filter(tenant=ctx.tenant, pk__in=delete).delete()

        sheet.saved_at = now
        fields = ["saved_at", "updated_at"]
        if sheet.status == SheetStatus.NOT_STARTED and marks:
            sheet.status = SheetStatus.IN_PROGRESS
            fields.append("status")
        sheet.save(update_fields=fields)


# ── Submitting ─────────────────────────────────────────────────────────────

def blank_cell_count(ctx: SheetContext) -> int:
    scores = (
        figures.stored_scores(ctx.tenant, [ctx.sheet.pk]).get(ctx.sheet.pk, {})
        if ctx.sheet is not None else {}
    )
    return sum(
        line.blank_cells
        for line in figures.lines(ctx.components, ctx.bands, ctx.students, scores)
    )


def submit(ctx: SheetContext) -> str:
    """Submit the sheet and answer where it went: ``MODERATION`` or ``CLASS_TEACHER``.

    The caller has already judged who may enter. Refused while it is locked or
    a cell is blank. A return note the sheet was carrying is cleared, because
    the correction it asked for has been made.
    """
    error = lock_error(ctx)
    if error is not None:
        raise error
    blanks = blank_cell_count(ctx)
    if blanks:
        noun = "cell is" if blanks == 1 else "cells are"
        raise BlanksRemain(
            f"{blanks} {noun} still blank. Fill every one, using ABS or Excused "
            f"where it applies, before submitting.",
            blank_count=blanks,
        )

    moderated = routes_to_moderation(ctx)
    now = timezone.now()
    with transaction.atomic():
        class_term, sheet = ensure_sheet(ctx, fix_structure=True)
        sheet = _lock(class_term, sheet)
        sheet.status = SheetStatus.SUBMITTED
        sheet.needs_moderation = moderated
        sheet.submitted_by, sheet.submitted_at = ctx.user, now
        sheet.returned_by, sheet.returned_at, sheet.returned_note = None, None, ""
        sheet.save(update_fields=[
            "status", "needs_moderation", "submitted_by", "submitted_at",
            "returned_by", "returned_at", "returned_note", "updated_at",
        ])
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="ScoreSheet", entity_id=sheet.pk, entity_label=label(ctx),
            action_type=AuditActionType.UPDATE,
            summary=(
                f"{ctx.user.full_name} submitted {label(ctx)}"
                f"{' for moderation' if moderated else ''}."
            ),
            class_id=ctx.school_class.pk, subject_id=ctx.subject.pk, term_id=ctx.term.pk,
            moderated=moderated,
        )
    return NEXT_MODERATION if moderated else NEXT_CLASS_TEACHER


def label(ctx: SheetContext) -> str:
    """How the trail and the notices name a sheet: subject, class and term."""
    return f"{ctx.subject.name}, {ctx.school_class.name}, {ctx.term.name}"
