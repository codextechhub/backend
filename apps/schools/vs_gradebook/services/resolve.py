"""Finding what a class's results are made of, and who is involved.

The gradebook reads these facts from the modules that own them and owns none of
them: the programme and level (academic structure), the subjects a level
offers and who takes the optional ones (:mod:`schools.vs_academics.services.electives`),
the class roll (:mod:`schools.vs_students.services.roster`), who teaches
what (staff management) and who heads a department
(:mod:`schools.vs_academics.services.heads`).

**Structure and scale in force.** A class is assessed by the structure and
graded by the scale that is IN_USE for its level's programme. One naming the
class's own branch wins; otherwise the school-wide one (null branch) applies.
A class with no branch of its own is the school's only branch's at a school with
one, and has only the school-wide structure and scale at a school with several.
When nothing is in use the answer is a refusal in words (``NO_STRUCTURE``,
``NO_SCALE``), never a guess.

**Due sheets.** A class's term has one sheet for every core subject its level
offers, and one for every optional subject at least one student takes.

**The sheet roll** is the class roll as at the earlier of the school's today and
the term's last day, so a term that has ended reads the roll it ended with, and
a term still running reads today's. A suspended student is on it. For an
optional subject it is narrowed to the students recorded as taking it.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.db.models import Q

from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    SessionStatus,
    SubjectOffering,
)
from schools.vs_academics.services import electives, heads
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import TeachingAssignment
from schools.vs_students.services.roster import roster_as_at
from vs_config.clock import branch_today, tenant_today
from vs_rbac.scoping import only_branch_id_or_several
from vs_tenants.models import Branch

from ..constants import SetupStatus
from ..exceptions import ClassHasNoBranch, InvalidTerm, NoScale, NoStructure
from ..models import AssessmentStructure, ClassTerm, GradingScale


def class_branch(tenant, school_class):
    """The branch a class's results belong to, or None at a school with several and a class with none."""
    if school_class.branch_id is not None:
        return school_class.branch
    only = only_branch_id_or_several(tenant)
    return Branch.all_objects.get(pk=only) if only is not None else None


def contact_phone(tenant, branch) -> str:
    """The number a family should call about a class's report cards; ``""`` when none is held.

    The class's branch phone when the branch has one, otherwise the school's own.
    The printed card and the email to the family go to an outside party, so this
    deliberately reads both numbers directly and is not narrowed by the school's
    Field Access switches on the branch or the school record (those govern what
    staff see in the app, not what the school says to a parent).
    """
    own = (getattr(branch, "phone", "") or "").strip()
    if own:
        return own
    school = getattr(tenant, "school_profile", None)
    return (getattr(school, "phone", "") or "").strip()


def school_today(tenant, school_class) -> dt.date:
    """Today at the class's branch, or at the school where the class has none."""
    branch = class_branch(tenant, school_class)
    return branch_today(tenant, branch) if branch is not None else tenant_today(tenant)


def _in_force(model, tenant, school_class, prefetch):
    """The IN_USE row of *model* for the class: its branch's own, else the school-wide one."""
    branch = class_branch(tenant, school_class)
    reach = Q(branch__isnull=True)
    if branch is not None:
        reach |= Q(branch=branch)
    candidates = list(
        model.objects.filter(
            reach, tenant=tenant, program_id=school_class.level.program_id,
            status=SetupStatus.IN_USE,
        ).prefetch_related(prefetch)
    )
    own = [c for c in candidates if branch is not None and c.branch_id == branch.pk]
    if own:
        return own[0]
    return next((c for c in candidates if c.branch_id is None), None)


def structure_for(tenant, school_class) -> AssessmentStructure:
    """The structure in force for *school_class*, with its components.

    Raises :class:`~..exceptions.NoStructure` when the programme has none in use.
    """
    found = _in_force(AssessmentStructure, tenant, school_class, "components")
    if found is None:
        raise NoStructure()
    return found


def scale_for(tenant, school_class) -> GradingScale:
    """The grading scale in force for *school_class*, with its bands.

    Raises :class:`~..exceptions.NoScale` when the programme has none in use.
    """
    found = _in_force(GradingScale, tenant, school_class, "bands")
    if found is None:
        raise NoScale()
    return found


def class_term(tenant, school_class, term, *, create: bool = False):
    """The :class:`ClassTerm` for the class and term, or None when none exists.

    With ``create`` a missing one is made, on the class's branch. Creating one
    for a term of another school year is refused (:class:`~..exceptions.InvalidTerm`),
    and so is a class with no branch at a school with several
    (:class:`~..exceptions.ClassHasNoBranch`). Reading never refuses.
    """
    found = ClassTerm.objects.filter(
        tenant=tenant, school_class=school_class, term=term,
    ).first()
    if found is not None or not create:
        return found
    if term.session_id != school_class.session_id:
        raise InvalidTerm(
            f"{term.name} is not a term of {school_class.session.name}, the year "
            f"{school_class.name} runs in. Choose one of that year's terms.",
        )
    branch = class_branch(tenant, school_class)
    if branch is None:
        raise ClassHasNoBranch()
    try:
        with transaction.atomic():
            return ClassTerm.objects.create(
                tenant=tenant, school_class=school_class, term=term, branch=branch,
            )
    except IntegrityError:
        return ClassTerm.objects.get(tenant=tenant, school_class=school_class, term=term)


def class_offerings(tenant, school_class) -> list:
    """The class's level's offerings of active subjects, by subject name, with the subject loaded."""
    return list(
        SubjectOffering.objects.filter(
            tenant=tenant, level_id=school_class.level_id, subject__is_active=True,
        )
        .select_related("subject", "subject__department")
        .order_by("subject__name", "pk")
    )


def due_subjects(tenant, school_class, term) -> list:
    """The subjects *school_class* owes a sheet for in *term*, by name.

    Every core subject its level offers, plus every optional subject at least
    one student takes in this class and term.
    """
    taken = electives.takers(tenant, school_class, term)
    return [
        offering.subject
        for offering in class_offerings(tenant, school_class)
        if electives.effective_is_core(offering) or taken.get(offering.subject_id)
    ]


def sheet_roll(tenant, school_class, term, subject) -> list:
    """The students on *subject*'s sheet: ``[RosterEntry]`` in name order.

    The class roll as at the earlier of the school's today and the term's last
    day, narrowed to the recorded takers when the subject is optional at the
    class's level. A subject the level does not offer has an empty roll.
    """
    offering = (
        SubjectOffering.objects.filter(
            tenant=tenant, level_id=school_class.level_id, subject_id=getattr(subject, "pk", subject),
        )
        .select_related("subject").first()
    )
    if offering is None:
        return []
    on_date = min(school_today(tenant, school_class), term.end_date)
    roll = roster_as_at(tenant, school_class, on_date)
    if electives.effective_is_core(offering):
        return roll
    taken = electives.takers(tenant, school_class, term).get(offering.subject_id, set())
    return [entry for entry in roll if entry.student.pk in taken]


def _assignments(school_class, subject, session, part):
    return (
        TeachingAssignment.objects.filter(
            tenant_id=school_class.tenant_id, school_class=school_class,
            subject_id=getattr(subject, "pk", subject),
            session_id=getattr(session, "pk", session), part=part,
        )
        .select_related("staff", "staff__user")
        .order_by("staff__user__last_name", "staff__user__first_name", "pk")
    )


def main_teacher(school_class, subject, session):
    """The staff record leading *subject* in *school_class* for *session*, or None."""
    row = _assignments(school_class, subject, session, TeachingPart.LEAD).first()
    return row.staff if row is not None else None


def assistants(school_class, subject, session) -> list:
    """The staff records assisting with *subject* in *school_class* for *session*, by name."""
    return [row.staff for row in _assignments(school_class, subject, session, TeachingPart.ASSISTANT)]


def head_of_subject(subject):
    """The staff record heading *subject*'s department, or None.

    None when the subject has no department, the department has no head, or the
    head has left the school's roll.
    """
    department = subject.department
    return heads.head_of(department) if department is not None else None


def current_term(tenant, today: dt.date):
    """The term to open on for *today*: the active school year's running term.

    Between terms it is the latest term begun, and before the first one the
    first, as the who-takes-what screen opens
    (:func:`schools.vs_academics.services.electives.default_term`). None when the
    school has no active year or the year has no terms.
    """
    session = (
        AcademicSession.objects.filter(tenant=tenant, status=SessionStatus.ACTIVE)
        .order_by("-start_date", "-pk").first()
    )
    if session is None:
        return None
    return electives.default_term(
        AcademicTerm.objects.filter(tenant=tenant, session=session), today,
    )
