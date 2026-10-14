"""Bulk upload of a score sheet: the template, the checked preview and the confirm.

**Why this is not the platform import engine.** The engine (``vs_import_data``)
creates new records from a file of one dataset type, in a background job that
can end half done and is undone by a rollback record. A score sheet is the
opposite on every count: it changes cells that may already hold a value and says
for each whether it is new, unchanged or a replacement; it is checked against
one sheet and one structure, not a dataset; and a confirm is all or nothing,
through the write the grid uses so every lock and rule applies again. Fitting
it would mean a new dataset type in the engine's own choices, a template row and
a handler that updates in place, and the engine's own batch keys would sit
beside the sheet's, none of which gives a teacher anything. So a checked upload
is a short-lived :class:`~..models.ScoreImport` in the gradebook instead.

**The template** is one row per student on the sheet's roll, in roll order, with
the current marks filled in where the caller may read marks.

**Checking** reads the file, reads its headings against the sheet's columns, and
judges every row. A heading problem is a problem of the file and stops the row
checks. A row is judged for the student it names (unknown admission number, not
on this sheet, named twice) and, cell by cell, by the rule the grid saves by
(:func:`.sheets.check_batch`), so a mark is refused in the same words wherever it
is typed. A blank cell in the file leaves the sheet's cell as it is: a blank is
never an instruction to erase.

**Confirming** saves the chosen rows that have no issue through
:func:`.sheets.save_scores`, in one transaction that first locks the sheet and
refuses if any stored cell differs from what the preview saw. The upload is used
once and expires after a day.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import NotFound

from schools.vs_students.models import Student
from schools.vs_students.services.scoping import scope_students
from vs_audit.models import AuditActionType
from vs_rbac.field_enforcement import can_read

from ..constants import (
    IMPORT_ADMISSION_HEADING,
    IMPORT_VALID_HOURS,
    MARK_FIELD,
    ImportIssue,
    ImportOutcome,
    Marker,
)
from ..exceptions import (
    CellsRefused,
    NoRowsChosen,
    RowHasIssues,
    SheetChangedSincePreview,
    UnknownUploadRow,
)
from ..models import Score, ScoreImport, ScoreSheet
from . import audit, figures, sheets, spreadsheet

_ADMISSION_HEADINGS = {"admission number", "admission no", "admission no.", "admission"}
_STUDENT_HEADINGS = {"student", "student name", "name"}
_MAX_IN_HEADING = re.compile(r"(.+?)\s*\(\s*max\s*(\d+)\s*\)")
_MARKERS = {Marker.ABSENT.value, Marker.EXCUSED.value}
NO_SUCH_UPLOAD = "No such upload. It may have expired, or already been confirmed."

BLANK, VALUE, UNREADABLE = "blank", "value", "unreadable"


def _issue(code: str, message: str, component=None) -> dict:
    found = {"code": code, "message": message}
    if component is not None:
        found["component"] = component
    return found


def _norm(text) -> str:
    return " ".join(str(text).replace("_", " ").casefold().split())


def _shown(mark, marker):
    """A cell as the preview prints it: the marker if there is one, else the mark."""
    return marker if marker else mark


# ── The template ───────────────────────────────────────────────────────────

def template_filename(ctx) -> str:
    """A file name naming the class, subject and term, safe for a download header."""
    stem = slugify(f"{ctx.subject.name} {ctx.school_class.name} {ctx.term.name}")
    return f"{stem or 'scores'}-scores.xlsx"


def template_bytes(ctx, request) -> bytes:
    """The sheet's workbook: every student on its roll, with marks filled in where the caller may read them."""
    stored = {}
    if ctx.sheet is not None and can_read(request, MARK_FIELD):
        stored = figures.stored_scores(ctx.tenant, [ctx.sheet.pk]).get(ctx.sheet.pk, {})
    rows = []
    for student in ctx.students:
        cells = []
        for component in ctx.components:
            mark, marker = stored.get((student.pk, component.pk), (None, None))
            cells.append(_shown(mark, marker))
        rows.append((student.student_number, student.full_name, cells))
    headings = [f"{c.name} (max {c.max_score})" for c in ctx.components]
    return spreadsheet.template_workbook(headings, rows)


# ── Reading the file against the sheet ─────────────────────────────────────

def _plan_columns(ctx, grid):
    """``(admission_index, {index: component}, file_issues)`` from the file's headings."""
    by_name = {}
    for component in ctx.components:
        by_name[_norm(component.name)] = component
    for component in ctx.components:
        by_name.setdefault(_norm(component.short_name), component)

    admission, mapped, issues, headless, stale = None, {}, [], [], set()
    for index, heading in enumerate(grid.headings):
        if heading is None:
            headless.append(index)
            continue
        text = _norm(heading)
        if text in _ADMISSION_HEADINGS:
            if admission is None:
                admission = index
            else:
                issues.append(_issue(
                    ImportIssue.UNKNOWN_COLUMN, f'The column "{heading}" appears twice.',
                ))
            continue
        if text in _STUDENT_HEADINGS:
            continue
        found = _MAX_IN_HEADING.fullmatch(text)
        component = by_name.get(found.group(1) if found else text)
        if component is None:
            issues.append(_issue(
                ImportIssue.UNKNOWN_COLUMN,
                f'The column "{heading}" is not part of this sheet. Download the '
                f"template again and fill that in.",
            ))
        elif found and int(found.group(2)) != component.max_score:
            issues.append(_issue(
                ImportIssue.UNKNOWN_COLUMN,
                f'The column "{heading}" was made for a maximum of {found.group(2)}, '
                f"but {component.name} is now out of {component.max_score}. Download "
                f"the template again.",
                component.pk,
            ))
            stale.add(component.pk)
        elif component.pk in {c.pk for c in mapped.values()}:
            issues.append(_issue(
                ImportIssue.UNKNOWN_COLUMN,
                f'The column "{heading}" appears twice.', component.pk,
            ))
        else:
            mapped[index] = component

    for index in headless:
        if any(values[index] is not None for _, values in grid.rows):
            issues.append(_issue(
                ImportIssue.UNKNOWN_COLUMN, "A column with no heading has values in it.",
            ))
            break
    if admission is None:
        issues.append(_issue(
            ImportIssue.MISSING_COLUMN,
            f'The "{IMPORT_ADMISSION_HEADING}" column is missing. Download '
            f"the template again and fill that in.",
        ))
    present = {component.pk for component in mapped.values()}
    for component in ctx.components:
        if component.pk not in present and component.pk not in stale:
            issues.append(_issue(
                ImportIssue.MISSING_COLUMN,
                f'The column for "{component.name}" is missing. Download the '
                f"template again and fill that in.",
                component.pk,
            ))
    return admission, mapped, issues


def _admission_text(value) -> str:
    """An admission number as text, whether the spreadsheet kept it as text or turned it into a number."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse(raw):
    """``(kind, mark, marker, shown)`` for one cell of the file.

    A mark is passed on as a number for the sheet's own rule to judge, so a
    decimal is refused as ``NOT_A_WHOLE_MARK`` in the grid's words and only text
    that is no number at all is unreadable.
    """
    if raw is None:
        return BLANK, None, None, None
    if isinstance(raw, bool):
        return UNREADABLE, None, None, str(raw)
    if isinstance(raw, int):
        return VALUE, raw, None, raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return UNREADABLE, None, None, str(raw)
        mark = int(raw) if raw.is_integer() else raw
        return VALUE, mark, None, mark
    if isinstance(raw, str):
        if raw.upper() in _MARKERS:
            return VALUE, None, raw.upper(), raw.upper()
        try:
            number = Decimal(raw)
        except InvalidOperation:
            return UNREADABLE, None, None, raw
        if not number.is_finite():
            return UNREADABLE, None, None, raw
        whole = number == number.to_integral_value()
        mark = int(number) if whole else float(number)
        return VALUE, mark, None, mark
    return UNREADABLE, None, None, str(raw)


def _students_named(ctx, texts) -> dict:
    """``{lower-cased admission number: student}`` for the numbers named, among students the caller may see.

    A student in a branch the caller cannot see is answered as unknown, so an
    upload cannot be used to learn who attends elsewhere.
    """
    wanted = {text.lower() for text in texts if text}
    if not wanted:
        return {}
    found = scope_students(
        Student.objects.filter(tenant=ctx.tenant).annotate(_lower=Lower("student_number"))
        .filter(_lower__in=wanted),
        ctx.user, ctx.tenant,
    )
    return {student.student_number.lower(): student for student in found}


def _not_on_sheet_message(ctx) -> str:
    if ctx.is_elective:
        return (
            f"This student is not on this sheet. {ctx.subject.name} is optional, and "
            f"only the students who take it in {ctx.school_class.name} are listed."
        )
    return f"This student is not in {ctx.school_class.name} on the roll for {ctx.term.name}."


def _row_level(entry) -> list:
    """The issues that are about the row's student, not about one of its cells."""
    return [issue for issue in entry["issues"] if "component" not in issue]


def check_rows(ctx, grid, admission_index, mapped) -> list:
    """The preview rows for a file whose headings are right, each with the cells it would write."""
    on_sheet = {student.pk for student in ctx.students}
    named = _students_named(ctx, [_admission_text(v[admission_index]) for _, v in grid.rows])
    stored = (
        figures.stored_scores(ctx.tenant, [ctx.sheet.pk]).get(ctx.sheet.pk, {})
        if ctx.sheet is not None else {}
    )

    entries, seen = [], {}
    for row_number, values in grid.rows:
        text = _admission_text(values[admission_index])
        student = named.get(text.lower()) if text else None
        entry = {
            "row_number": row_number, "admission_number": text,
            "student": {"id": student.pk, "name": student.full_name} if student else None,
            "cells": [], "issues": [], "writes": [],
            "_student": student, "_values": values,
        }
        if not text:
            entry["issues"].append(_issue(
                ImportIssue.UNKNOWN_ADMISSION_NUMBER, "This row has no admission number.",
            ))
        elif student is None:
            entry["issues"].append(_issue(
                ImportIssue.UNKNOWN_ADMISSION_NUMBER,
                f'No student here has the admission number "{text}".',
            ))
        elif student.pk not in on_sheet:
            entry["issues"].append(_issue(
                ImportIssue.NOT_ON_THIS_SHEET, _not_on_sheet_message(ctx),
            ))
        seen.setdefault(student.pk if student else ("text", text.lower()), []).append(row_number)
        entries.append(entry)

    for entry in entries:
        student = entry["_student"]
        key = student.pk if student else ("text", entry["admission_number"].lower())
        others = [n for n in seen[key] if n != entry["row_number"]]
        if entry["admission_number"] and others:
            entry["issues"].append(_issue(
                ImportIssue.DUPLICATE_ROW,
                "This student is also on row " + ", ".join(str(n) for n in others)
                + ". Keep one row for each student.",
            ))

    parsed, candidates = {}, []
    for entry in entries:
        values = entry["_values"]
        cells = {}
        for index, component in mapped.items():
            kind, mark, marker, shown = _parse(values[index])
            cells[component.pk] = (kind, mark, marker, shown)
            if kind == UNREADABLE:
                entry["issues"].append(_issue(
                    ImportIssue.UNREADABLE_VALUE,
                    f'"{shown}" is not a mark. Use a whole number, ABS or EXC.', component.pk,
                ))
        parsed[entry["row_number"]] = cells
        if entry["_student"] is not None and not _row_level(entry):
            candidates += [
                {"student": entry["_student"].pk, "component": component_id,
                 "mark": mark, "marker": marker}
                for component_id, (kind, mark, marker, _) in cells.items() if kind == VALUE
            ]

    refused = {}
    if candidates:
        try:
            sheets.check_batch(on_sheet, ctx.components, candidates)
        except CellsRefused as error:
            for problem in error.extra["cells"]:
                refused[(problem["student"], problem["component"])] = problem

    for entry in entries:
        student = entry["_student"]
        blocked = bool(_row_level(entry))
        for component in ctx.components:
            kind, mark, marker, shown = parsed[entry["row_number"]].get(
                component.pk, (BLANK, None, None, None),
            )
            existing = stored.get((student.pk, component.pk), (None, None)) if student else (None, None)
            problem = refused.get((student.pk, component.pk)) if student else None
            if problem is not None:
                entry["issues"].append(_issue(problem["code"], problem["message"], component.pk))
                outcome = None
            elif blocked or any(i.get("component") == component.pk for i in entry["issues"]):
                outcome = None
            elif kind == BLANK:
                outcome = ImportOutcome.BLANK
            elif (mark, marker) == existing:
                outcome = ImportOutcome.SAME
            elif existing == (None, None):
                outcome = ImportOutcome.NEW
            else:
                outcome = ImportOutcome.REPLACES
            entry["cells"].append({
                "component": component.pk,
                "value": shown,
                "outcome": outcome.value if outcome else None,
                "existing": _shown(*existing),
            })
            if outcome in (ImportOutcome.NEW, ImportOutcome.REPLACES):
                entry["writes"].append([student.pk, component.pk, mark, marker])
        if entry["issues"]:
            entry["writes"] = []
    for entry in entries:
        del entry["_student"], entry["_values"]
    return entries


# ── The preview ────────────────────────────────────────────────────────────

def fingerprint(tenant, sheet_id) -> str:
    """A digest of every stored cell of the sheet, or of the empty sheet when it has none."""
    digest = hashlib.sha256()
    if sheet_id is not None:
        cells = Score.objects.filter(tenant=tenant, sheet_id=sheet_id).order_by(
            "student_id", "component_id",
        ).values_list("student_id", "component_id", "mark", "marker")
        for cell in cells:
            digest.update(repr(cell).encode())
    return digest.hexdigest()


def _summary(entries) -> dict:
    ready = [entry for entry in entries if not entry["issues"]]
    return {
        "rows": len(entries), "ready": len(ready), "with_issues": len(entries) - len(ready),
        "replaces": sum(
            1 for entry in ready for cell in entry["cells"]
            if cell["outcome"] == ImportOutcome.REPLACES.value
        ),
    }


def preview(ctx, upload) -> dict:
    """Check an uploaded file against the sheet and keep the result for a confirm.

    Writes nothing to the sheet. A file whose headings do not fit the sheet
    comes back with ``file_issues`` and no token, because there is nothing to
    confirm. Otherwise the answer carries the token, when it expires, the
    summary and every row.
    """
    grid = spreadsheet.read_grid(upload)
    admission, mapped, file_issues = _plan_columns(ctx, grid)
    if file_issues:
        return {
            "token": None, "expires_at": None,
            "summary": {"rows": 0, "ready": 0, "with_issues": 0, "replaces": 0},
            "file_issues": file_issues, "rows": [],
        }
    entries = check_rows(ctx, grid, admission, mapped)

    now = timezone.now()
    ScoreImport.objects.filter(tenant=ctx.tenant, expires_at__lt=now).delete()
    batch = ScoreImport.objects.create(
        tenant=ctx.tenant, token=secrets.token_urlsafe(32), user=ctx.user,
        school_class=ctx.school_class, subject=ctx.subject, term=ctx.term,
        structure=ctx.structure,
        fingerprint=fingerprint(ctx.tenant, ctx.sheet.pk if ctx.sheet else None),
        file_name=(getattr(upload, "name", "") or "")[:255],
        rows=entries, expires_at=now + timedelta(hours=IMPORT_VALID_HOURS),
    )
    return {
        "token": batch.token, "expires_at": batch.expires_at,
        "summary": _summary(entries), "file_issues": [],
        "rows": [{k: v for k, v in entry.items() if k != "writes"} for entry in entries],
    }


# ── The confirm ────────────────────────────────────────────────────────────

def find(tenant, user, token, class_id, subject_id) -> ScoreImport:
    """The unused, unexpired upload *user* made for this sheet, else a 404 that says nothing more."""
    batch = (
        ScoreImport.objects.filter(
            tenant=tenant, user=user, token=token, school_class_id=class_id,
            subject_id=subject_id, confirmed_at__isnull=True, expires_at__gt=timezone.now(),
        ).select_related("term").first()
    )
    if batch is None:
        raise NotFound(NO_SUCH_UPLOAD)
    return batch


def confirm(ctx, batch: ScoreImport, row_numbers) -> dict:
    """Save the chosen rows of a checked upload, all or nothing, and answer what was done.

    Refused, with nothing saved, when no row is chosen, a chosen row is not in the
    upload or has an issue, the sheet is locked, the sheet's columns or stored
    marks differ from what the preview saw, or the grid's own rule refuses a cell
    now (a student who has since left the roll). A row whose cells are all
    unchanged or blank saves nothing and is not an error.
    """
    chosen = list(dict.fromkeys(row_numbers))
    if not chosen:
        raise NoRowsChosen()
    by_number = {entry["row_number"]: entry for entry in batch.rows}
    unknown = [n for n in chosen if n not in by_number]
    if unknown:
        raise UnknownUploadRow(rows=unknown)
    blocked = [n for n in chosen if by_number[n]["issues"]]
    if blocked:
        raise RowHasIssues(rows=blocked)
    if ctx.structure.pk != batch.structure_id:
        raise SheetChangedSincePreview()

    error = sheets.lock_error(ctx)
    if error is not None:
        raise error
    cells = [
        {"student": student, "component": component, "mark": mark, "marker": marker}
        for number in chosen
        for student, component, mark, marker in by_number[number]["writes"]
    ]
    replaced = sum(
        1 for number in chosen for cell in by_number[number]["cells"]
        if cell["outcome"] == ImportOutcome.REPLACES.value
    )

    with transaction.atomic():
        used = ScoreImport.objects.select_for_update().get(pk=batch.pk)
        if used.confirmed_at is not None:
            raise NotFound(NO_SUCH_UPLOAD)
        sheet = (
            ScoreSheet.objects.select_for_update()
            .filter(tenant=ctx.tenant, class_term__school_class=ctx.school_class,
                    class_term__term=ctx.term, subject=ctx.subject).first()
        )
        if fingerprint(ctx.tenant, sheet.pk if sheet else None) != batch.fingerprint:
            raise SheetChangedSincePreview()
        sheets.save_scores(ctx, cells)
        used.confirmed_at = timezone.now()
        used.save(update_fields=["confirmed_at", "updated_at"])
        record = ScoreSheet.objects.filter(
            tenant=ctx.tenant, class_term__school_class=ctx.school_class,
            class_term__term=ctx.term, subject=ctx.subject,
        ).first() or used
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type=type(record).__name__, entity_id=record.pk,
            entity_label=sheets.label(ctx), action_type=AuditActionType.UPDATE,
            summary=(
                f"{ctx.user.full_name} uploaded scores for {sheets.label(ctx)} from a "
                f"spreadsheet: {len(chosen)} rows, {len(cells)} scores"
                f"{f', {replaced} replacing earlier scores' if replaced else ''}."
            ),
            class_id=ctx.school_class.pk, subject_id=ctx.subject.pk, term_id=ctx.term.pk,
            rows=len(chosen), scores=len(cells), replaced=replaced, file_name=batch.file_name,
        )
    return {"rows": len(chosen), "scores": len(cells), "replaced": replaced}
