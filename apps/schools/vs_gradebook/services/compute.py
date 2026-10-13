"""The arithmetic of a result, as pure functions that touch no database.

Everything a sheet, a broadsheet and a report card shows that is worked out
rather than entered comes from here, so the rule has one home and a screen can
never disagree with the card. Nothing here reads a model: the callers pass
plain values, or objects with the few attributes named below, and get plain
values back. All money-like arithmetic is :class:`~decimal.Decimal`; no float
is ever used.

**A cell** is ``(mark, marker)``. ``mark`` is a whole number or None, ``marker``
is ``"ABS"``, ``"EXC"`` or None. A cell with neither is blank, and a blank is
never a zero.

**A row** is one student's cells for the components of a structure, in the
structure's order.

* A mark counts as itself.
* ABS counts as zero and **keeps** its component's maximum in what the student
  could obtain: the student missed the paper, and the paper was worth marks.
* EXC removes the component from what the student could obtain: the student
  was excused, so the paper is not held against them. Excusing a 20-mark first
  test of a 100-mark structure leaves 80 obtainable, and a student with 61
  marks is graded at 61 out of 80, which is 76.25 percent.
* A row with any blank cell is **incomplete**: it has no total, no
  percentage, no grade and no remark.
* A row with every component excused has no percentage and no grade, and is
  left out of the student's overall average.

**The grade is chosen from the unrounded percentage.** A student on 74.996
percent is shown 75.00 and is graded in the band below 75. The percentage a
screen prints is rounded with :func:`round2`; the one grading uses never is.

**Positions share on ties at two decimal places and skip**: 1st, 2nd, 3rd, 3rd,
5th (:func:`positions`).

A *component* is anything with ``max_score``. A *band* is anything with
``min_score``, ``grade``, ``remark`` and ``is_fail``; ``scale_check`` also
reads ``grade`` and ``is_fail`` only. The ORM rows satisfy both, and so do the
small dataclasses below, which tests and snapshots use.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..constants import STRUCTURE_TOTAL_MARKS, Marker

ABSENT = Marker.ABSENT.value
EXCUSED = Marker.EXCUSED.value

_HUNDRED = Decimal(100)
_TWO_PLACES = Decimal("0.01")
_ONE_PLACE = Decimal("0.1")

#: What the editor shows next to a scale that is sound.
SCALE_OK_MESSAGE = "No gap, no overlap, 0 to 100 covered."
#: What the editor shows next to a structure that is sound.
STRUCTURE_OK_MESSAGE = "The components total 100."


@dataclass(frozen=True)
class Component:
    """A column of a structure, as the arithmetic needs it."""

    max_score: int
    is_exam: bool = False


@dataclass(frozen=True)
class Band:
    """A band of a scale, as the arithmetic needs it."""

    min_score: int
    grade: str
    remark: str = ""
    is_fail: bool = False


@dataclass(frozen=True)
class RowResult:
    """One student's worked-out line for one subject.

    ``total`` and ``obtainable`` are whole marks. ``percentage`` is unrounded.
    The grade fields are None whenever there is nothing to grade: a blank
    somewhere in the row, or every component excused. ``complete`` is False
    when a cell is blank. ``all_excused`` and ``excused_any`` let a screen print
    "61 out of 80 obtainable, graded at 76%".
    """

    total: int | None
    obtainable: int | None
    percentage: Decimal | None
    grade: str | None
    remark: str | None
    is_fail: bool | None
    complete: bool
    all_excused: bool
    excused_any: bool


@dataclass(frozen=True)
class ClassStats:
    """A subject's figures across a class, from the students' totals."""

    average: Decimal | None
    highest: int | None
    lowest: int | None


@dataclass(frozen=True)
class SheetFigures:
    """A subject's figures across a class, from its students' graded rows.

    ``average`` is the mean of the percentages, unrounded; ``highest`` and
    ``lowest`` are totals; ``pass_rate`` is the whole percentage of graded rows
    that fall outside a fail band.
    """

    average: Decimal | None
    highest: int | None
    lowest: int | None
    pass_rate: int | None
    graded: int


@dataclass(frozen=True)
class Check:
    """The verdict on a structure or a scale, with the sentence an editor shows."""

    ok: bool
    message: str


def round2(value: Decimal | None) -> Decimal | None:
    """*value* to two decimal places, halves rounding up. None stays None."""
    if value is None:
        return None
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def round1(value: Decimal | None) -> Decimal | None:
    """*value* to one decimal place, halves rounding up. None stays None."""
    if value is None:
        return None
    return value.quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)


def band_for(percentage: Decimal | None, bands: Iterable):
    """The band an unrounded *percentage* falls in, or None.

    The band is the one with the highest lowest-mark that does not exceed the
    percentage. None when there is no percentage, no bands, or the percentage is
    below every band (a scale that does not start at 0, which a scale in use
    never is).
    """
    if percentage is None:
        return None
    best = None
    for band in bands:
        if band.min_score <= percentage and (best is None or band.min_score > best.min_score):
            best = band
    return best


def row_result(
    components: Sequence, cells: Sequence[tuple[int | None, str | None]], bands: Iterable,
) -> RowResult:
    """One student's line for a subject. See the module docstring for the rules.

    *cells* are in the same order as *components*.
    """
    if len(components) != len(cells):
        raise ValueError("A row needs exactly one cell for each component.")

    excused = [marker == EXCUSED for _, marker in cells]
    excused_any = any(excused)
    blank = any(mark is None and marker is None for mark, marker in cells)
    if blank:
        return RowResult(
            total=None, obtainable=None, percentage=None, grade=None, remark=None,
            is_fail=None, complete=False, all_excused=False, excused_any=excused_any,
        )

    total = sum(
        (mark or 0) for (mark, marker) in cells if marker is None
    )
    obtainable = sum(
        component.max_score
        for component, (_, marker) in zip(components, cells)
        if marker != EXCUSED
    )
    if obtainable == 0:
        return RowResult(
            total=0, obtainable=0, percentage=None, grade=None, remark=None,
            is_fail=None, complete=True, all_excused=True, excused_any=excused_any,
        )

    percentage = Decimal(total) * _HUNDRED / Decimal(obtainable)
    band = band_for(percentage, bands)
    return RowResult(
        total=total, obtainable=obtainable, percentage=percentage,
        grade=band.grade if band else None,
        remark=band.remark if band else None,
        is_fail=band.is_fail if band else None,
        complete=True, all_excused=False, excused_any=excused_any,
    )


def overall_average(subject_percentages: Iterable[Decimal | None]) -> Decimal | None:
    """The mean of the percentages that exist, unrounded; None when none does.

    A subject the student does not take, has not had entered, or was excused
    from entirely passes None and is left out, not counted as zero.
    """
    present = [value for value in subject_percentages if value is not None]
    if not present:
        return None
    return sum(present, Decimal(0)) / Decimal(len(present))


def positions(averages: dict) -> dict:
    """``{key: position}`` for each key of *averages*, ranked highest first.

    Averages equal to two decimal places share a position and the next position
    is skipped, so four students on 80, 75, 75 and 60 are 1st, 2nd, 2nd and 4th.
    A key whose average is None has no position (None).
    """
    ranked = {
        key: round2(value) for key, value in averages.items() if value is not None
    }
    ordered = sorted(ranked.values(), reverse=True)
    out = {key: None for key in averages}
    for key, value in ranked.items():
        out[key] = 1 + sum(1 for other in ordered if other > value)
    return out


def class_stats(totals: Iterable[int | None]) -> ClassStats:
    """The class average (to one decimal place), highest and lowest of the totals.

    None totals (incomplete rows) are left out. With no totals, all three are
    None.
    """
    present = [total for total in totals if total is not None]
    if not present:
        return ClassStats(average=None, highest=None, lowest=None)
    mean = Decimal(sum(present)) / Decimal(len(present))
    return ClassStats(average=round1(mean), highest=max(present), lowest=min(present))


def sheet_figures(results: Iterable[RowResult]) -> SheetFigures:
    """The class figures of a sheet from its students' rows.

    Only a **graded** row counts (it has a percentage): a row with a blank has
    none, and a row excused from every component is left out as it is from the
    student's average. With nothing graded every figure is None.
    """
    graded = [result for result in results if result.percentage is not None]
    if not graded:
        return SheetFigures(None, None, None, None, 0)
    totals = [result.total for result in graded]
    passes = sum(1 for result in graded if not result.is_fail)
    rate = (Decimal(passes) * _HUNDRED / Decimal(len(graded))).quantize(
        Decimal(1), rounding=ROUND_HALF_UP,
    )
    return SheetFigures(
        average=overall_average(result.percentage for result in graded),
        highest=max(totals), lowest=min(totals), pass_rate=int(rate), graded=len(graded),
    )


def scale_check(bands: Iterable) -> Check:
    """Whether *bands* grade every mark from 0 to 100, with the sentence an editor shows.

    A band runs from its lowest mark up to the next band's, so the bands cover
    0 to 100 with no gap and no overlap exactly when the lowest band starts at 0
    and every lowest mark is different. A scale also needs a grade on every band
    and at least one band that is not a fail, or nobody could pass.
    """
    bands = list(bands)
    if not bands:
        return Check(False, "Add at least one band.")
    for band in bands:
        if not 0 <= band.min_score <= 100:
            return Check(False, "A band's lowest mark must be between 0 and 100.")
    if any(not str(band.grade or "").strip() for band in bands):
        return Check(False, "Every band needs a grade.")

    seen = set()
    for band in sorted(bands, key=lambda b: b.min_score):
        if band.min_score in seen:
            return Check(
                False,
                f"Two bands start at {band.min_score}, so they overlap. "
                f"Give each band its own lowest mark.",
            )
        seen.add(band.min_score)

    lowest = min(seen)
    if lowest != 0:
        return Check(
            False,
            f"The lowest band starts at {lowest}, so marks below it have no grade. "
            f"Make the lowest band start at 0.",
        )
    if all(band.is_fail for band in bands):
        return Check(False, "Every band is a fail. Mark at least one band as a pass.")
    return Check(True, SCALE_OK_MESSAGE)


def structure_total(components: Iterable) -> int:
    """The marks the components add up to."""
    return sum(component.max_score for component in components)


def structure_check(components: Iterable) -> Check:
    """Whether *components* can be put in use: they total 100 and at most one is the examination."""
    components = list(components)
    if not components:
        return Check(False, "Add at least one component.")
    total = structure_total(components)
    if total != STRUCTURE_TOTAL_MARKS:
        return Check(
            False,
            f"The components add up to {total}, not {STRUCTURE_TOTAL_MARKS}. "
            f"Adjust the maximums until they total {STRUCTURE_TOTAL_MARKS}.",
        )
    if sum(1 for component in components if component.is_exam) > 1:
        return Check(False, "Only one component can be the examination. Untick the others.")
    return Check(True, STRUCTURE_OK_MESSAGE)
