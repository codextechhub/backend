"""Who takes which non-core subject, class by class and term by term.

**A core subject is everyone's.** It has no taker rows and :func:`takes` answers
True for every student. **A non-core subject is taken only by the students
recorded for it** in that class and term, so a student with no row takes no part
in it: they are on no score sheet, no average and no position for that subject.
Whether a subject is core at a level is the offering's call when it has one and
the subject's own otherwise (:func:`effective_is_core`).

Other modules read the answer through :func:`non_core_offerings`,
:func:`takers` and :func:`takes`, and never query the table themselves, so the
rule has one home.

**The roll.** The students a class can record choices for are the ones on its
roll as at the earlier of today and the term's last day
(:func:`~schools.vs_students.services.roster.roster_as_at`). Nobody is excluded:
a suspended child is on it, because suspension does not change what they study.

**Changing a choice** goes through :func:`apply_changes`, which validates the
whole request before it writes anything and writes it in one transaction, so a
request with one bad row changes nothing. A subject must be a non-core offering
of the class's level, the student must be on the roll, and the term must belong
to the class's school year.

**Removal guards.** Taking a student out of a subject can undo work that rests
on the choice, such as scores already entered for them. This module cannot know
about that work, and the modules that hold it import this one, so the guard is
registered the other way round: an app appends a callable to
:data:`REMOVAL_GUARDS` from its ``AppConfig.ready()``. Each guard is called as
``guard(tenant, school_class, term, subject, student_id)`` for every removal of
a recorded choice and returns a sentence saying why the removal is refused, or
None to allow it. The first sentence returned refuses the whole request.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from django.db import transaction
from rest_framework.exceptions import NotFound

from ..exceptions import ElectiveRefused, ElectiveRemovalRefused
from ..models import AcademicTerm, ElectiveTaker, SubjectOffering
from .reads import term_state
from .years import assert_year_is_writable

#: Callables ``guard(tenant, school_class, term, subject, student_id) -> str | None``
#: that may refuse the removal of a recorded choice. See the module docstring.
REMOVAL_GUARDS: list[Callable] = []


def register_removal_guard(guard: Callable) -> None:
    """Add *guard* to :data:`REMOVAL_GUARDS` once, however often it is registered."""
    if guard not in REMOVAL_GUARDS:
        REMOVAL_GUARDS.append(guard)


def effective_is_core(offering) -> bool:
    """Whether the subject is core at the offering's level.

    The offering's own flag when it is set, else the subject's. Needs the
    offering's subject, so select it with the offering.
    """
    return offering.is_core if offering.is_core is not None else offering.subject.is_core


def non_core_offerings(school_class) -> list:
    """The class's level's offerings of active subjects that are not core, by subject name."""
    offerings = (
        SubjectOffering.objects.filter(
            tenant_id=school_class.tenant_id, level_id=school_class.level_id,
            subject__is_active=True,
        )
        .select_related("subject")
        .order_by("subject__name", "pk")
    )
    return [o for o in offerings if not effective_is_core(o)]


def takers(tenant, school_class, term) -> dict:
    """``{subject_id: {student_id, ...}}`` for every recorded choice in the class and term.

    Only subjects with at least one taker appear. A core subject never appears:
    it has no rows. Use :func:`takes` to ask about one student.
    """
    out: dict[int, set] = {}
    rows = ElectiveTaker.objects.filter(
        tenant=tenant, school_class=school_class, term=term,
    ).values_list("subject_id", "student_id")
    for subject_id, student_id in rows:
        out.setdefault(subject_id, set()).add(student_id)
    return out


def takes(tenant, school_class, term, subject, student_id) -> bool:
    """Whether *student_id* takes *subject* in the class and term.

    *subject* is a subject or its id. A core subject at the class's level is
    everyone's, so True. A non-core subject is True only for a recorded taker. A
    subject the level does not offer is False for everybody.
    """
    subject_id = getattr(subject, "pk", subject)
    offering = (
        SubjectOffering.objects.filter(
            tenant=tenant, level_id=school_class.level_id, subject_id=subject_id,
        )
        .select_related("subject").first()
    )
    if offering is None:
        return False
    if effective_is_core(offering):
        return True
    return ElectiveTaker.objects.filter(
        tenant=tenant, school_class=school_class, term=term,
        subject_id=subject_id, student_id=student_id,
    ).exists()


def default_term(terms, today: dt.date):
    """The term to open on: the one running today, else the latest begun, else the first.

    *terms* are the class's school year's terms. None when the year has none.
    """
    terms = sorted(terms, key=lambda t: (t.order_index, t.pk))
    for term in terms:
        if term_state(term, today) == "ongoing":
            return term
    begun = [t for t in terms if t.start_date <= today]
    if begun:
        return begun[-1]
    return terms[0] if terms else None


def resolve_term(tenant, school_class, term_id, today: dt.date):
    """The term a request is about, which must belong to the class's school year.

    A term of another school is a 404, as is an unknown one. A term of this
    school's other years is refused in words. With no term named, the class's
    year's current term.
    """
    if term_id in (None, ""):
        term = default_term(
            AcademicTerm.objects.filter(tenant=tenant, session_id=school_class.session_id),
            today,
        )
        if term is None:
            raise ElectiveRefused(
                f"{school_class.session.name} has no terms yet, so there is nothing to "
                f"record. Add its terms first.",
            )
        return term
    try:
        term = AcademicTerm.objects.get(tenant=tenant, pk=int(term_id))
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise NotFound("No such term at this school.")
    if term.session_id != school_class.session_id:
        raise ElectiveRefused(
            f"{term.name} is not a term of {school_class.session.name}, the year "
            f"{school_class.name} runs in.",
        )
    return term


def roll_as_at(tenant, school_class, term, today: dt.date) -> list:
    """The class's roll for *term*: ``[RosterEntry]``, read as at the earlier of today and the term's end."""
    from schools.vs_students.services.roster import roster_as_at

    return roster_as_at(tenant, school_class, min(today, term.end_date))


def class_term_payload(tenant, school_class, term, today: dt.date) -> dict:
    """Everything the who-takes-what screen shows for one class and one term.

    ``subjects`` are the non-core offerings with how many of the roll take each;
    ``students`` are the roll in name order with the ids of the subjects each
    takes. A class with no non-core subject has an empty ``subjects`` list and
    every student's ``takes`` is empty.
    """
    roll = roll_as_at(tenant, school_class, term, today)
    on_roll = {entry.student.pk for entry in roll}
    recorded = takers(tenant, school_class, term)
    offerings = non_core_offerings(school_class)
    subject_ids = {o.subject_id for o in offerings}

    subjects = [
        {
            "id": o.subject_id,
            "name": o.subject.name,
            "code": o.subject.code,
            "takers": len(recorded.get(o.subject_id, set()) & on_roll),
            "roll": len(roll),
        }
        for o in offerings
    ]
    students = [
        {
            "id": entry.student.pk,
            "name": entry.student.full_name,
            "admission_number": entry.student.student_number,
            "takes": sorted(
                subject_id for subject_id in subject_ids
                if entry.student.pk in recorded.get(subject_id, ())
            ),
        }
        for entry in roll
    ]
    return {
        "class": {"id": school_class.pk, "name": school_class.name},
        "term": {"id": term.pk, "name": term.name},
        "subjects": subjects,
        "students": students,
    }


def apply_changes(tenant, school_class, term, changes, *, actor, today: dt.date) -> dict:
    """Record or remove choices, all or nothing, and say what changed.

    *changes* is a list of ``{"student": id, "subject": id, "takes": bool}``. A
    pair named twice takes its last value. Everything is checked before
    anything is written: the school year must still be writable, the term must
    be the class's year's, every subject must be a non-core offering of the
    class's level and every student on the roll (``ElectiveRefused``, naming the
    position of the first fault in ``extra['index']``), and every removal of a
    recorded choice must pass :data:`REMOVAL_GUARDS` (``ElectiveRemovalRefused``).

    Returns ``{"added": [(student_id, subject_id)], "removed": [...]}``; a
    request that changes nothing returns two empty lists.
    """
    assert_year_is_writable(school_class.session)
    if term.session_id != school_class.session_id:
        raise ElectiveRefused(
            f"{term.name} is not a term of {school_class.session.name}, the year "
            f"{school_class.name} runs in.",
        )

    offered = {o.subject_id: o for o in non_core_offerings(school_class)}
    core_names = {
        o.subject_id: o.subject.name
        for o in SubjectOffering.objects.filter(
            tenant=tenant, level_id=school_class.level_id,
        ).select_related("subject")
        if effective_is_core(o)
    }
    roll = {entry.student.pk: entry.student for entry in roll_as_at(tenant, school_class, term, today)}

    wanted: dict[tuple, bool] = {}
    for index, change in enumerate(changes):
        subject_id, student_id = change["subject"], change["student"]
        if subject_id in core_names:
            raise ElectiveRefused(
                f"{core_names[subject_id]} is a core subject at {school_class.level.name}, "
                f"so every student takes it.",
                index=index,
            )
        if subject_id not in offered:
            raise ElectiveRefused(
                f"That subject is not an elective at {school_class.level.name}.",
                index=index,
            )
        if student_id not in roll:
            raise ElectiveRefused(
                f"That student is not on the {school_class.name} roll for {term.name}.",
                index=index,
            )
        wanted[(student_id, subject_id)] = bool(change["takes"])

    existing = {
        (row.student_id, row.subject_id): row
        for row in ElectiveTaker.objects.filter(
            tenant=tenant, school_class=school_class, term=term,
        )
    }
    to_add = [pair for pair, on in wanted.items() if on and pair not in existing]
    to_remove = [pair for pair, on in wanted.items() if not on and pair in existing]

    for student_id, subject_id in to_remove:
        for guard in REMOVAL_GUARDS:
            refusal = guard(tenant, school_class, term, offered[subject_id].subject, student_id)
            if refusal:
                raise ElectiveRemovalRefused(refusal, student=student_id, subject=subject_id)

    with transaction.atomic():
        ElectiveTaker.objects.bulk_create([
            ElectiveTaker(
                tenant=tenant, school_class=school_class, term=term,
                subject_id=subject_id, student_id=student_id, set_by=actor,
            )
            for student_id, subject_id in to_add
        ], ignore_conflicts=True)
        if to_remove:
            ElectiveTaker.objects.filter(
                pk__in=[existing[pair].pk for pair in to_remove],
            ).delete()
    return {"added": to_add, "removed": to_remove}

