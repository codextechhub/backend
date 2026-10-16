"""A pupil's own results in the portal: one term, the whole session, the card.

**A pupil reads released results only**: published and not held, at the
latest version. A term that is published but held answers ``withheld`` with a
sentence telling the pupil to ask the school office, and nothing else: no
reason, no balance, no figures. A term not yet published answers
``not_published``. Both are ordinary answers, not errors, because a pupil
looking at the Results screen in July should be told where their Third Term
card is rather than shown an error.

Every figure is read from the frozen snapshot the gradebook published, so the
portal and the printed card cannot disagree. Position appears only where the
school ranks, and the class average only where it shows class figures: a
school that does neither shows neither, rather than a dash.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

RELEASED, WITHHELD, NOT_PUBLISHED = "released", "withheld", "not_published"

WITHHELD_SENTENCE = (
    "Your result for this term is being held by the school. Ask the school "
    "office when it will be available."
)


def _results(student):
    """The pupil's published results, newest term last, with their class-terms."""
    from schools.vs_gradebook.constants import StoredStage
    from schools.vs_gradebook.models import StudentTermResult

    return list(
        StudentTermResult.objects.filter(
            tenant=student.tenant, student=student, snapshot__isnull=False,
            class_term__stage=StoredStage.PUBLISHED,
        )
        .select_related("class_term__term__session", "class_term__branch")
        .order_by("class_term__term__start_date", "class_term__term__pk")
    )


def _state(result) -> str:
    return RELEASED if result.released_at is not None else WITHHELD


def _term_ref(term) -> dict:
    return {"id": term.pk, "name": term.name, "session": term.session.name}


def term_choices(student) -> list:
    """Every term the pupil has a published result for, with its state."""
    return [
        {**_term_ref(r.class_term.term), "state": _state(r)}
        for r in _results(student)
    ]


def _pick(student, term_id):
    """The published result for *term_id*, or the latest one; None when there is none."""
    results = _results(student)
    if term_id is None:
        released = [r for r in results if r.released_at is not None]
        return (released or results or [None])[-1], results
    try:
        wanted = int(term_id)
    except (TypeError, ValueError):
        raise NotFound("No such term at this school.") from None
    match = next((r for r in results if r.class_term.term_id == wanted), None)
    if match is None:
        from schools.vs_academics.models import AcademicTerm

        if not AcademicTerm.objects.filter(tenant=student.tenant, pk=wanted).exists():
            raise NotFound("No such term at this school.")
    return match, results


def _position_label(position, roll_size) -> str | None:
    from schools.vs_gradebook.services.reportcards import position_label

    return position_label(position, roll_size) if position is not None else None


def term_result(student, term_id=None) -> dict:
    """One term's result as the pupil reads it, or the state that stands in for it."""
    result, results = _pick(student, term_id)
    choices = [{**_term_ref(r.class_term.term), "state": _state(r)} for r in results]
    if result is None:
        return {"state": NOT_PUBLISHED, "term": None, "terms": choices}
    body = {"term": _term_ref(result.class_term.term), "terms": choices}
    if result.released_at is None:
        return {**body, "state": WITHHELD, "message": WITHHELD_SENTENCE}

    snap = result.snapshot
    components = snap.get("components") or []
    by_component = {c["id"]: c for c in components}
    subjects = []
    for row in snap.get("subjects") or []:
        subjects.append({
            "name": row["subject"]["name"],
            "teacher": row["subject"].get("teacher") or None,
            "cells": [
                {
                    "short_name": by_component.get(cell["component"], {}).get("short_name", ""),
                    "max_score": by_component.get(cell["component"], {}).get("max_score"),
                    "mark": cell["mark"],
                    "marker": cell["marker"],
                }
                for cell in row.get("cells") or []
            ],
            "total": row.get("total"),
            "obtainable": row.get("obtainable"),
            "percentage": row.get("percentage"),
            "grade": row.get("grade"),
            "remark": row.get("remark"),
        })
    summary = {
        "overall_average": snap.get("overall_average"),
        "subjects": len(subjects),
    }
    if snap.get("position") is not None:
        summary["position"] = snap["position"]
        summary["position_label"] = _position_label(snap["position"], snap.get("roll_size"))
    if snap.get("class_average") is not None:
        summary["class_average"] = snap["class_average"]
    return {
        **body,
        "state": RELEASED,
        "released_at": result.released_at,
        "version": result.version,
        "class_name": snap["class"]["name"],
        "components": [
            {"short_name": c["short_name"], "max_score": c["max_score"], "is_exam": c["is_exam"]}
            for c in components
        ],
        "subjects": subjects,
        "summary": summary,
        "teacher_comment": snap.get("teacher_comment") or "",
        "class_teacher": snap.get("class_teacher"),
        "next_term_begins": snap.get("next_term_begins"),
    }


def session_results(student, session_id=None) -> dict:
    """The whole school year: each term's figures, and every subject across it.

    Read from the latest released term of the year. Where that term is the
    year's last (it carries the cumulative block), the block gives each
    subject's three terms and annual average, the annual average and, where the
    school ranks, the annual position; the promotion decision is read live from
    Student Management. Before the last term is released, the terms released so
    far are listed and ``cumulative`` is null. The class teacher's remark on the
    year's last card is the session's remark; the gradebook keeps one remark
    per term and no separate annual one.
    """
    from schools.vs_gradebook.services.reportcards import promotion_lines_for

    results = _results(student)
    if session_id is not None:
        try:
            wanted = int(session_id)
        except (TypeError, ValueError):
            raise NotFound("No such school year.") from None
        results = [r for r in results if r.class_term.term.session_id == wanted]
    elif results:
        latest_session = results[-1].class_term.term.session_id
        results = [r for r in results if r.class_term.term.session_id == latest_session]
    if not results:
        return {"state": NOT_PUBLISHED, "session": None, "terms": [], "cumulative": None}

    session = results[-1].class_term.term.session
    terms = []
    for r in results:
        entry = {**_term_ref(r.class_term.term), "state": _state(r)}
        if r.released_at is not None:
            snap = r.snapshot
            entry["average"] = snap.get("overall_average")
            if snap.get("position") is not None:
                entry["position_label"] = _position_label(snap["position"], snap.get("roll_size"))
            if snap.get("class_average") is not None:
                entry["class_average"] = snap["class_average"]
        terms.append(entry)

    released = [r for r in results if r.released_at is not None]
    last = released[-1] if released else None
    block = (last.snapshot or {}).get("cumulative") if last is not None else None
    cumulative = None
    if block:
        names = {t["id"]: t["name"] for t in block.get("terms") or []}
        cumulative = {
            "terms": [{"id": t["id"], "name": t["name"]} for t in block.get("terms") or []],
            "subjects": [
                {
                    "name": row["subject"]["name"],
                    "terms": [
                        {"term": names.get(t["term_id"], ""), "percentage": t.get("percentage")}
                        for t in row.get("terms") or []
                    ],
                    "annual_average": row.get("annual_average"),
                }
                for row in block.get("subjects") or []
            ],
            "annual_average": (block.get("overall") or {}).get("annual_average"),
            "promotion": promotion_lines_for(student.tenant, session.pk, [last]).get(student.pk),
            "remark": (last.snapshot or {}).get("teacher_comment") or "",
            "class_teacher": (last.snapshot or {}).get("class_teacher"),
        }
        if block.get("annual_position") is not None:
            cumulative["annual_position_label"] = _position_label(
                block["annual_position"], block.get("annual_position_of"),
            )
    return {
        "state": RELEASED if released else WITHHELD,
        "session": {"id": session.pk, "name": session.name},
        "terms": terms,
        "terms_released": len(released),
        "cumulative": cumulative,
        "next_term_begins": (last.snapshot or {}).get("next_term_begins") if last else None,
    }


def report_card(student, term_id, *, user=None) -> dict:
    """The released card for *term_id*, as the gradebook prints it; 404 otherwise.

    A held or unpublished term answers 404 rather than a card, so a card that is
    withheld from the family can never be printed from the portal.
    """
    from schools.vs_gradebook.services.reportcards import card, letterhead, promotion_lines_for

    if term_id is None:
        raise NotFound("Name the term whose report card you want.")
    result, _ = _pick(student, term_id)
    if result is None or result.released_at is None:
        raise NotFound("No report card is available for this term.")
    class_term = result.class_term
    return card(
        result.snapshot,
        letterhead_block=letterhead(student.tenant, class_term.branch, user),
        held=False, version=result.version,
        promotion=promotion_lines_for(
            student.tenant, class_term.term.session_id, [result],
        ).get(student.pk),
    )
