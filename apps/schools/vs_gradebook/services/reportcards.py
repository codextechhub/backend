"""The printed report card, a class's cards for printing and the release list.

**A card is the snapshot, never a recomputation.** Publishing froze each
student's figures, comments, names and grading key on
``StudentTermResult.snapshot``; this module turns that document into the lines a
card prints. A scale, a sheet, a remark template or a setting changed after
publication changes nothing here. Only the letterhead is read live, because the
school's name and logo and the branch's address and phone are the school's present
identity and not part of a result.

**The lines.** ``facts``, ``totals`` and ``attendance`` are lists of
``{label, value}`` with the value already worded for print ("75.00%", "3rd of
24"), so a screen prints them in order and has nothing to format. Each ``totals``
line carries ``governed_by``, the name of the switch that decides whether a
caller may read the figure it states (:data:`..constants.CARD_GOVERNED_BY`); the serializer drops
the line when that switch is closed and removes the marker, because a line such
as "Position in class" built from a hidden position would state the position
the switch withholds.

**The year block.** The last term of a school year with more than one term has a
``cumulative`` block in its snapshot (:mod:`.cumulative`), and its card gains
the year's term totals, annual averages and annual position, and the promotion
line Student Management has recorded (:mod:`.promotion_line`), read live because
the decision is made after the card is issued.

**What is optional.** The position line exists only when the snapshot has a
position, which is only when the school ranked. Class figures exist only when the
school showed them at publication. Ratings and their key exist only when the
school recorded conduct ratings. "Next term begins" exists only when the school
had a next term. A key that is absent is absent from the card, never blank.

**Who may see a card.** A card exists only for a class that is published
(``NOT_PUBLISHED`` otherwise). A held student's card is still readable by staff
and says so (``held``); a class's print leaves held students out and counts them.

**The release list** names, for every student on the class's roll and every
student the class published, the primary guardian, the state of the result and
what became of the guardian's email. A released result whose email has no
recorded outcome reads ``NOT_SENT``, never as sent.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from rest_framework.exceptions import NotFound

from core.media import signed_url
from schools.vs_attendance.services.notices import primary_guardians
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import (
    EMAIL_NOT_SENT,
    RATING_KEY_TEXT,
    EmailState,
    LeftOut,
    ResultState,
    StoredStage,
)
from ..exceptions import NotPublished
from ..models import ClassTerm, ResultHold, StudentTermResult
from . import book, promotion_line, resolve
from .classbook import ClassContext

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_ONE_PLACE = Decimal("0.1")


# ── Wording ────────────────────────────────────────────────────────────────

def ordinal(number: int) -> str:
    """*number* as an English ordinal: 1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 22nd."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def day_label(day: dt.date) -> str:
    """*day* as a card prints it: ``Mon 5 Jan 2026``."""
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]} {day.year}"


def position_label(position, roll_size) -> str:
    """``3rd of 24``."""
    return f"{ordinal(position)} of {roll_size}"


def _one_place(text) -> str | None:
    if text is None:
        return None
    return str(Decimal(str(text)).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP))


def _cell_text(cell) -> str:
    """A component cell as printed: the mark, ``ABS`` or ``Exc``."""
    if cell.get("marker") == "ABS":
        return "ABS"
    if cell.get("marker") == "EXC":
        return "Exc"
    mark = cell.get("mark")
    return "-" if mark is None else str(mark)


def grading_key_line(key) -> str:
    """``A 70+ · B 60+ · ...`` from the snapshot's bands, highest first."""
    return " · ".join(f"{band['grade']} {band['min_score']}+" for band in key)


# ── The letterhead ─────────────────────────────────────────────────────────

def letterhead(tenant, branch, user=None) -> dict:
    """The school's present identity for the top of a card.

    The school's name comes from its record, the logo from its branding as a URL
    signed for the reader, and the address and phone from the class's branch,
    each falling back to the school's own (the phone line is left off when
    neither has one), so a family calls the branch that holds the card. The
    letterhead is a document for an outside party and is deliberately not
    narrowed by the school's Field Access switches on branch contact details. The branch is named only at a school with
    several, where it says which site the card comes from; at a school with one
    it would repeat the school.
    """
    school = getattr(tenant, "school_profile", None)
    logo = None
    branding = getattr(school, "branding", None) if school is not None else None
    if branding is not None and getattr(branding, "logo", None):
        try:
            logo = signed_url(branding.logo.name, user=user) or None
        except Exception:
            logo = None
    several = only_branch_id_or_several(tenant) is None
    address = (getattr(branch, "address", "") or "") or (getattr(school, "address", "") or "")
    return {
        "school_name": school.name if school is not None else tenant.name,
        "logo_url": logo,
        "branch_name": branch.name if several and branch is not None else None,
        "branch_address": address or None,
        "phone": resolve.contact_phone(tenant, branch) or None,
    }


# ── One card ───────────────────────────────────────────────────────────────

def _line(label, value, governed_by=None) -> dict:
    line = {"label": label, "value": value}
    if governed_by is not None:
        line["governed_by"] = governed_by
    return line


def _subject_row(subject, show_stats) -> dict:
    row = {
        "name": subject["subject"]["name"],
        "cells": [_cell_text(cell) for cell in subject["cells"]],
        "total": subject["total"], "grade": subject["grade"], "remark": subject["remark"],
    }
    if show_stats:
        row["class_average"] = _one_place(subject.get("class_average"))
        row["highest"] = subject.get("highest")
        row["lowest"] = subject.get("lowest")
    return row


def cumulative_lines(block, promotion=None) -> dict:
    """The year block of a cumulative snapshot as a card and the Results tab print it.

    ``terms`` are the term names in order. Each of ``subjects`` has its ``name``,
    ``totals`` (one per term in the same order, ``""`` where the term has none)
    and ``annual_average``. ``annual_average`` is the overall figure; the
    ``annual_position`` line ("7th of 32") exists only where the snapshot ranked
    and the pupil has a position, and ``promotion_line`` only when Student
    Management has recorded an outcome. Averages are two-place figures without a
    percent sign, as the Results tab's overall average is.
    """
    out = {
        "terms": [each["name"] for each in block["terms"]],
        "subjects": [
            {
                "name": row["subject"]["name"],
                "totals": ["" if each["total"] is None else str(each["total"]) for each in row["terms"]],
                "annual_average": row["annual_average"],
            }
            for row in block["subjects"]
        ],
        "annual_average": block["overall"]["annual_average"],
    }
    if block.get("annual_position") is not None:
        out["annual_position"] = position_label(
            block["annual_position"], block.get("annual_position_of"),
        )
    if promotion:
        out["promotion_line"] = promotion
    return out


def card(snapshot, *, letterhead_block, held, version, promotion=None) -> dict:
    """The report card for *snapshot*, before Field Access is applied.

    Pure: nothing is read but the snapshot, the letterhead and, for a cumulative
    term, the promotion line handed in.
    """
    options = snapshot.get("options") or {}
    show_stats = bool(options.get("show_class_stats"))
    term, session = snapshot["term"], snapshot["session"]
    average = snapshot.get("overall_average")

    totals = [
        _line("Total obtained", str(snapshot["total_obtained"]), "row_result"),
        _line("Total obtainable", str(snapshot["total_obtainable"]), "row_result"),
        _line("Overall average", f"{average}%" if average is not None else "-", "overall_average"),
    ]
    if snapshot.get("position") is not None:
        totals.append(_line(
            "Position in class",
            position_label(snapshot["position"], snapshot.get("roll_size")), "position",
        ))

    attendance = snapshot.get("attendance") or {}
    body = {
        "letterhead": letterhead_block,
        "term_line": f"Report card · {term['name']}, {session['name']}",
        "facts": [
            _line("Student", snapshot["student"]["name"]),
            _line("Admission no.", snapshot["student"]["admission_number"]),
            _line("Class", snapshot["class"]["name"]),
            _line("Number in class", str(snapshot["class_size"])),
        ],
        "components": [
            {"short_name": c["short_name"], "max_score": c["max_score"]}
            for c in snapshot["components"]
        ],
        "subjects": [
            _subject_row(subject, show_stats) for subject in snapshot["subjects"]
        ],
        "totals": totals,
        "attendance": [
            _line("Times school opened", str(attendance.get("opened", 0))),
            _line("Times present", str(attendance.get("present", 0))),
            _line("Times absent", str(attendance.get("absent", 0))),
        ],
        "teacher_comment": snapshot["teacher_comment"],
        "teacher_name": (
            f"{snapshot['class_teacher']}, class teacher" if snapshot.get("class_teacher") else None
        ),
        "principal_comment": snapshot.get("principal_comment"),
        "principal_name": snapshot.get("principal"),
        "grading_key": grading_key_line(snapshot["grading_key"]),
        "version": version,
        "held": held,
    }
    if "ratings" in snapshot:
        ratings = snapshot["ratings"] or {}
        traits = snapshot.get("traits") or list(ratings)
        body["ratings"] = [
            {"trait": trait, "value": ratings[trait]} for trait in traits if trait in ratings
        ]
        if options.get("rating_key"):
            body["rating_key"] = RATING_KEY_TEXT
    if snapshot.get("cumulative"):
        body["cumulative"] = cumulative_lines(snapshot["cumulative"], promotion)
    if snapshot.get("next_term_begins"):
        body["next_term_line"] = "Next term begins " + day_label(
            dt.date.fromisoformat(snapshot["next_term_begins"]),
        )
    return body


# ── Reading a published class ──────────────────────────────────────────────

def published_term(ctx: ClassContext) -> ClassTerm:
    """The class's term row, refused (``NOT_PUBLISHED``) unless it is published."""
    class_term = ctx.class_term
    if class_term is None or class_term.stage != StoredStage.PUBLISHED:
        raise NotPublished()
    return class_term


def _name_key(student):
    return (student.last_name.lower(), student.first_name.lower(), student.pk)


def _published_results(tenant, class_term) -> list:
    """The class-term's published results, students loaded, in roll (name) order."""
    rows = list(
        StudentTermResult.objects.filter(
            tenant=tenant, class_term=class_term, snapshot__isnull=False,
        ).select_related("student")
    )
    rows.sort(key=lambda row: _name_key(row.student))
    return rows


def _is_held(result) -> bool:
    return result.released_at is None


def promotion_lines_for(tenant, session_id, results) -> dict:
    """``{student_id: promotion line}`` for those of *results* whose snapshot is cumulative.

    Read live from Student Management (:mod:`.promotion_line`), for the whole
    list at once. A result that is not cumulative is not looked up.
    """
    return promotion_line.promotion_lines(
        tenant, session_id,
        [row.student_id for row in results if (row.snapshot or {}).get("cumulative")],
    )


def one_card(ctx: ClassContext, student_id, user=None) -> dict:
    """A published student's card, or a 404 for a student the class did not publish."""
    class_term = published_term(ctx)
    result = StudentTermResult.objects.filter(
        tenant=ctx.tenant, class_term=class_term, student_id=student_id,
        snapshot__isnull=False,
    ).first()
    if result is None:
        raise NotFound("No such student on this class's report cards.")
    return card(
        result.snapshot, letterhead_block=letterhead(ctx.tenant, ctx.branch, user),
        held=_is_held(result), version=result.version,
        promotion=promotion_lines_for(ctx.tenant, ctx.term.session_id, [result]).get(
            result.student_id,
        ),
    )


def class_cards(ctx: ClassContext, user=None) -> dict:
    """Every released student's card in roll order, and who was left out and why."""
    class_term = published_term(ctx)
    head = letterhead(ctx.tenant, ctx.branch, user)
    results = _published_results(ctx.tenant, class_term)
    released = [r for r in results if r.released_at is not None]
    promotion = promotion_lines_for(ctx.tenant, ctx.term.session_id, released)
    cards = [
        card(
            r.snapshot, letterhead_block=head, held=False, version=r.version,
            promotion=promotion.get(r.student_id),
        )
        for r in released
    ]
    published_ids = {r.student_id for r in results}
    left_out = [
        {"student": _student_ref(r.student), "reason": LeftOut.HELD}
        for r in results if r.released_at is None
    ]
    roll = book.rolls_for(ctx.tenant, ctx.term, [(ctx.school_class, ctx.branch)])
    for entry in roll.get(ctx.school_class.pk, []):
        if entry.student.pk not in published_ids:
            left_out.append({
                "student": _student_ref(entry.student), "reason": LeftOut.NOT_RELEASED,
            })
    left_out.sort(key=lambda row: row["student"]["name"].lower())
    return {
        "class": {"id": ctx.school_class.pk, "name": ctx.school_class.name},
        "term": {"id": ctx.term.pk, "name": ctx.term.name},
        "cards": cards, "left_out": left_out, "left_out_count": len(left_out),
    }


def _student_ref(student) -> dict:
    return {"id": student.pk, "name": student.full_name, "admission_number": student.student_number}


# ── The release list ───────────────────────────────────────────────────────

def _email_state(result) -> str | None:
    """What the list says of the email: HELD, NO_EMAIL, SENT, or NOT_SENT when none was recorded."""
    if result.released_at is None:
        return EmailState.HELD
    return result.email_state or EMAIL_NOT_SENT


def release_list(ctx: ClassContext) -> dict:
    """The class's release list: each student's guardian, result state and email state.

    The rows are every student on the class's roll and every student the class
    published (one who left the class after publication still has a card), by
    name. Before publication every row is ``NOT_PUBLISHED`` and has no email
    state. ``held_count`` is the students whose result is held, and before
    publication the students on the roll with a hold open, who will be held.
    """
    entry = book.load(ctx.tenant, ctx.term, [(ctx.school_class, ctx.branch)])[0]
    class_term = entry.class_term
    published = class_term is not None and class_term.stage == StoredStage.PUBLISHED
    results = (
        {
            r.student_id: r
            for r in StudentTermResult.objects.filter(
                tenant=ctx.tenant, class_term=class_term, snapshot__isnull=False,
            ).select_related("student")
        }
        if published else {}
    )
    students = {roll_entry.student.pk: roll_entry.student for roll_entry in entry.roll}
    for student_id, result in results.items():
        students.setdefault(student_id, result.student)
    ordered = sorted(students.values(), key=_name_key)
    reach = primary_guardians(ctx.tenant, [student.pk for student in ordered])
    open_holds = set(
        ResultHold.objects.filter(
            tenant=ctx.tenant, term=ctx.term, lifted_at__isnull=True,
            student_id__in=list(students),
        ).values_list("student_id", flat=True)
    )

    rows, held = [], 0
    for student in ordered:
        result = results.get(student.pk)
        state, email, emailed_at, released_at = ResultState.NOT_PUBLISHED, None, None, None
        if result is not None and result.released_at is not None:
            state, email = ResultState.RELEASED, _email_state(result)
            emailed_at, released_at = result.emailed_at, result.released_at
        elif result is not None:
            state, email = ResultState.HELD, _email_state(result)
            held += 1
        elif not published and student.pk in open_holds:
            held += 1
        rows.append({
            "student": _student_ref(student),
            "guardian": reach.get(student.pk),
            "result_state": state, "email_state": email,
            "emailed_at": emailed_at, "released_at": released_at,
        })
    published_sizes = [r.snapshot["class_size"] for r in results.values()]
    return {
        "class": {"id": ctx.school_class.pk, "name": ctx.school_class.name},
        "term": {"id": ctx.term.pk, "name": ctx.term.name},
        "stage": entry.stage,
        "published_at": class_term.published_at if published else None,
        "class_size": published_sizes[0] if published_sizes else len(entry.roll),
        "held_count": held, "rows": rows,
    }
