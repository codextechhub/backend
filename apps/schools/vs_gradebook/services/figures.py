"""A sheet's students worked out line by line.

The one place a roll, a structure's components, a scale's bands and the stored
scores become each student's cells and result. The score sheet, the moderation
queue and (later) the broadsheet and the report card all read through it, so a
total is the same number on every screen.

A student with no stored score for a component has a blank cell there. Scores of
students who are no longer on the roll are never passed in, so they are neither
shown nor counted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..models import Score
from . import compute

_WHOLE = Decimal(1)


@dataclass(frozen=True)
class Line:
    """One student on a sheet: their cells in component order and the result."""

    student: object
    cells: tuple
    result: compute.RowResult

    @property
    def blank_cells(self) -> int:
        return sum(1 for mark, marker in self.cells if mark is None and marker is None)


def stored_scores(tenant, sheet_ids) -> dict:
    """``{sheet_id: {(student_id, component_id): (mark, marker)}}`` for the sheets named."""
    out: dict[int, dict] = {sheet_id: {} for sheet_id in sheet_ids}
    if not out:
        return out
    rows = Score.objects.filter(tenant=tenant, sheet_id__in=list(out)).values_list(
        "sheet_id", "student_id", "component_id", "mark", "marker",
    )
    for sheet_id, student_id, component_id, mark, marker in rows:
        out[sheet_id][(student_id, component_id)] = (mark, marker)
    return out


def lines(components, bands, students, scores) -> list[Line]:
    """A line for each of *students*, from the *scores* of their sheet.

    *students* are in roll order and each has a ``pk``; *scores* maps
    ``(student_id, component_id)`` to ``(mark, marker)``.
    """
    bands = list(bands)
    out = []
    for student in students:
        cells = tuple(
            scores.get((student.pk, component.pk), (None, None)) for component in components
        )
        out.append(Line(student, cells, compute.row_result(components, cells, bands)))
    return out


def whole_percent(value: Decimal) -> int:
    """*value* rounded to a whole number, halves up."""
    return int(value.quantize(_WHOLE, rounding=ROUND_HALF_UP))


def excused_note(result: compute.RowResult) -> str | None:
    """The sentence a row carries when a component was excused, else None.

    "61 out of 80 obtainable, graded at 76%": the total, what the student could
    obtain once the excused component was taken out, and the whole percentage
    they were graded at. A row excused from everything says it is not graded. An
    incomplete row says nothing, because it has no total yet.
    """
    if not result.complete or not result.excused_any:
        return None
    if result.all_excused:
        return "Excused from every component, so not graded."
    return (
        f"{result.total} out of {result.obtainable} obtainable, "
        f"graded at {whole_percent(result.percentage)}%"
    )
