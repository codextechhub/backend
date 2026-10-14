"""The cumulative block of a snapshot: the whole school year on the last term's card.

A term is **cumulative** when it is the last term of its school year by its
order and the year has more than one term. Publishing such a term adds a
``cumulative`` block to every student's snapshot::

    {
      "terms": [{"id", "name"}, ...],                    # the year's terms, in order
      "subjects": [{
          "subject": {"id", "name"},
          "terms": [{"term_id", "total", "percentage"}], # one entry per term, in order
          "annual_average": "72.40" | null,
      }],
      "overall": {
          "terms": [{"term_id", "average"}],
          "annual_average": "70.15" | null,
      },
      "annual_position": 3 | null, "annual_position_of": 24,   # only where the school ranks
    }

**Where the earlier terms' figures come from.** From the student's own latest
published snapshot of each earlier term of the year, whatever class they were
in then: a pupil who moved from JSS1 B to JSS1 A after the first term still
finds the first term's marks on the third term's card. A term nothing was
published for, or a subject the pupil did not have in that term, is null. The
snapshots of a whole class are read in one query (:func:`earlier_figures`),
never one per student, and only the two keys the block needs are read out of
them.

**The arithmetic.** A subject's annual average is the mean of the term
percentages that exist, and the overall annual average is the mean of the term
overall averages that exist, both worked out from the two-place figures the
snapshots carry and shown to two places. The annual position ranks the class by
its overall annual average with ties shared and the next place skipped
(:func:`.compute.positions`), among the students who have one, and exists only
where the school ranks.

**Staying right.** The block is a function of the snapshot's own term figures
and the earlier terms' figures it already holds, so an amendment refreshes it
without reading another term (:func:`refreshed`): the amended student's term
entries, annual averages and annual position against their classmates' published
annual averages. No classmate's block changes.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from schools.vs_academics.models import AcademicTerm

from ..constants import StoredStage
from ..models import StudentTermResult
from . import compute
from .classbook import percentage_of


# ── Which term, and which terms ────────────────────────────────────────────

def session_terms(tenant, term) -> list:
    """The terms of *term*'s school year, in their order."""
    return list(
        AcademicTerm.objects.filter(tenant=tenant, session_id=term.session_id)
        .order_by("order_index", "start_date", "pk")
    )


def is_cumulative(terms, term) -> bool:
    """Whether *term* is the last of *terms* and the year has more than one."""
    return len(terms) > 1 and terms[-1].pk == term.pk


# ── The arithmetic (no database) ───────────────────────────────────────────

def mean(figures: Iterable[str | None]) -> str | None:
    """The mean of the two-place figures that exist, shown to two places; None when none does."""
    return percentage_of(
        compute.overall_average(Decimal(figure) for figure in figures if figure is not None)
    )


@dataclass(frozen=True)
class Earlier:
    """One student's published figures for one earlier term.

    ``subjects`` maps a subject id to its ``(total, percentage)`` and
    ``overall`` is the term's overall average.
    """

    subjects: dict
    overall: str | None


def block_for(terms, term, subjects, overall_average, earlier) -> dict:
    """The cumulative block for one student, without a position.

    *terms* is the year's terms in order and *term* the one being published.
    *subjects* are the snapshot's subject rows, *overall_average* its overall
    average and *earlier* the student's :class:`Earlier` figures by term id.
    """
    rows = []
    for row in subjects:
        subject_id = row["subject"]["id"]
        entries = []
        for each in terms:
            if each.pk == term.pk:
                total, percentage = row["total"], row["percentage"]
            else:
                found = earlier.get(each.pk)
                total, percentage = (
                    found.subjects.get(subject_id, (None, None)) if found else (None, None)
                )
            entries.append({"term_id": each.pk, "total": total, "percentage": percentage})
        rows.append({
            "subject": dict(row["subject"]),
            "terms": entries,
            "annual_average": mean(entry["percentage"] for entry in entries),
        })
    averages = [
        overall_average if each.pk == term.pk
        else (earlier[each.pk].overall if each.pk in earlier else None)
        for each in terms
    ]
    return {
        "terms": [{"id": each.pk, "name": each.name} for each in terms],
        "subjects": rows,
        "overall": {
            "terms": [
                {"term_id": each.pk, "average": average}
                for each, average in zip(terms, averages)
            ],
            "annual_average": mean(averages),
        },
    }


def _annual_of(block) -> Decimal | None:
    figure = block["overall"]["annual_average"]
    return Decimal(figure) if figure is not None else None


def rank(blocks: dict, roll_size: int) -> None:
    """Give each block in ``{student_id: block}`` its annual position and the roll it is out of.

    Ranked by overall annual average among the students who have one; a student
    without one has no position.
    """
    placed = compute.positions({student_id: _annual_of(block) for student_id, block in blocks.items()})
    for student_id, block in blocks.items():
        block["annual_position"] = placed[student_id]
        block["annual_position_of"] = roll_size


def refreshed(block, term_id, subjects, overall_average, classmates) -> dict:
    """*block* after the term *term_id*'s figures changed to *subjects* and *overall_average*. Pure.

    The term's entries and both annual averages are worked out again from the
    figures the block already holds for the other terms. Where the block ranks,
    the annual position is the student's new overall annual average against the
    annual averages their *classmates'* blocks were published with, so it can
    share a place with a classmate. *block* is not changed.
    """
    new = copy.deepcopy(block)
    current = {row["subject"]["id"]: row for row in subjects}
    for row in new["subjects"]:
        found = current.get(row["subject"]["id"])
        for entry in row["terms"]:
            if entry["term_id"] == term_id and found is not None:
                entry["total"], entry["percentage"] = found["total"], found["percentage"]
        row["annual_average"] = mean(entry["percentage"] for entry in row["terms"])
    for entry in new["overall"]["terms"]:
        if entry["term_id"] == term_id:
            entry["average"] = overall_average
    new["overall"]["annual_average"] = mean(entry["average"] for entry in new["overall"]["terms"])
    if "annual_position" in new:
        published = {
            n: _annual_of(other)
            for n, other in enumerate(classmates)
            if _annual_of(other) is not None
        }
        new["annual_position"] = compute.positions({**published, "me": _annual_of(new)})["me"]
    return new


# ── Reading the earlier terms ──────────────────────────────────────────────

def earlier_figures(tenant, earlier_terms, student_ids) -> dict:
    """``{student_id: {term_id: Earlier}}`` for the students' published results of *earlier_terms*.

    One query for the whole class. A student with two published results for one
    term (they changed class during it) is read from the one published last,
    and from its latest snapshot, so an approved correction is what is read.
    """
    out: dict = {}
    if not earlier_terms or not student_ids:
        return out
    rows = (
        StudentTermResult.objects.filter(
            tenant=tenant, student_id__in=list(student_ids),
            class_term__term_id__in=[each.pk for each in earlier_terms],
            class_term__stage=StoredStage.PUBLISHED, snapshot__isnull=False,
        )
        .order_by("class_term__published_at", "pk")
        .values_list(
            "student_id", "class_term__term_id", "snapshot__subjects",
            "snapshot__overall_average",
        )
    )
    for student_id, term_id, subjects, overall in rows:
        out.setdefault(student_id, {})[term_id] = Earlier(
            subjects={
                row["subject"]["id"]: (row["total"], row["percentage"])
                for row in subjects or []
            },
            overall=overall,
        )
    return out


def attach(tenant, terms, term, snapshots: dict, *, ranks: bool, roll_size: int) -> None:
    """Add the cumulative block to every snapshot in ``{student_id: snapshot}``.

    Called while a cumulative term is published. The earlier terms are read once
    for the class (:func:`earlier_figures`).
    """
    earlier = earlier_figures(tenant, [each for each in terms if each.pk != term.pk], list(snapshots))
    blocks = {
        student_id: block_for(
            terms, term, snapshot["subjects"], snapshot["overall_average"],
            earlier.get(student_id, {}),
        )
        for student_id, snapshot in snapshots.items()
    }
    if ranks:
        rank(blocks, roll_size)
    for student_id, block in blocks.items():
        snapshots[student_id]["cumulative"] = block
