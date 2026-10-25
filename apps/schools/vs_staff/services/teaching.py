"""Who teaches what, and who owns a class.

This module says **what** somebody teaches and nothing about **when**. A day, a
period or a room here would make two tables answer where a teacher is on
Wednesday afternoon, and the clash rules read only one of them. If the timetable
slips, the answer is to ship the timetable, not to grow a day column.

An assignment carries a part. A lead owns the subject in that class, there is at
most one, and the partial unique constraint is what makes "no lead" a countable
state rather than a judgement. Assistants are unbounded, because a split class
genuinely has several teachers.

FRD M12 v2.1, FR-011 and FR-017.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..constants import TeachingPart
from ..exceptions import (
    ClassTeachersNotAtBranch,
    LeadAlreadySet,
    SessionArchived,
    StaffHasLeft,
    TeacherNotAtBranch,
    TeacherStillHoldsClasses,
)
from . import audit


def assert_session_open(session):
    """An archived year takes no new teaching, and still returns its old.

    Who taught what last year is the record a school will be asked for, so
    reading is never refused; only writing is.
    """
    from schools.vs_academics.models import SessionStatus

    if session.status == SessionStatus.ARCHIVED:
        raise SessionArchived(
            f"{session.name} has been archived, so its teaching cannot be "
            f"changed. Its existing assignments are still readable.",
            session=session.name,
        )


def _full_name(user) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p).strip() or "This teacher"


def _names(people: list) -> str:
    """"A", "A and B", "A, B and C", or "A, B, C and 2 others"."""
    if len(people) > 3:
        people = people[:3] + [f"{len(people) - 3} others"]
    if len(people) == 1:
        return people[0]
    return f"{', '.join(people[:-1])} and {people[-1]}"


def _several_branches(tenant) -> bool:
    from vs_rbac.scoping import only_branch_id_or_several

    return only_branch_id_or_several(tenant) is None


def assert_teacher_reaches_class(tenant, user, school_class):
    """Refuse a teacher whose roles do not cover *school_class*'s branch.

    A teacher sees the classes at the branches their roles cover, and being put
    on a class does not widen that. A Main Branch teacher made class teacher of
    an Annex class would hold it on paper and never find it: no register, no
    score sheet, no class page. So the appointment is refused and the message
    names the branch to give them a role at. A teacher who works at both holds
    the Teacher role at both.

    Nothing is checked at a school with one branch, or for a class shared
    across a school with several: neither has a branch for the teacher to be
    missing from.
    """
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
    from vs_tenants.models import Branch

    if user is None or school_class.branch_id is None or not _several_branches(tenant):
        return
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT or school_class.branch_id in visible:
        return
    name = _full_name(user)
    branch = Branch.all_objects.get(pk=school_class.branch_id).name
    raise TeacherNotAtBranch(
        f"{name} does not work at {branch}. Give them the Teacher role there first.",
        teacher=name, branch=branch,
    )


def assert_still_reaches_own_classes(tenant, user):
    """Refuse a change to *user*'s roles that hides a class they still teach.

    The reach guard :mod:`vs_rbac.reach_guards` runs after a grant is revoked,
    moved or replaced, inside the same transaction, so the reach read here is
    the one the change leaves behind and raising undoes the change. A class
    counts when this person leads it, holds a subject duty in it, or has a
    lesson in it on the timetable, in any year not yet archived. The message
    names the branch and the classes; ``extra`` carries what is held in each.
    """
    from schools.vs_academics.models import SchoolClass, SessionStatus
    from schools.vs_calendar.models import TimetableSlot
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
    from vs_user.models import User

    from ..models import TeachingAssignment

    if not _several_branches(tenant):
        return
    # A fresh instance: the reach memo on the one in hand predates the change.
    fresh = User.objects.filter(pk=user.pk).first()
    if fresh is None:
        return
    visible = visible_branch_ids(fresh, tenant)
    if visible is WHOLE_TENANT:
        return

    live = {"tenant": tenant}
    held = {}
    for school_class in (
        SchoolClass.all_objects.filter(class_teacher__user_id=user.pk, **live)
        .exclude(session__status=SessionStatus.ARCHIVED)
        .filter(branch__isnull=False).exclude(branch_id__in=visible)
        .select_related("branch")
    ):
        held.setdefault(school_class.branch.name, {}).setdefault(school_class.name, []).append(
            "class teacher",
        )
    for duty in (
        TeachingAssignment.all_objects.filter(staff__user_id=user.pk, **live)
        .exclude(session__status=SessionStatus.ARCHIVED)
        .filter(school_class__branch__isnull=False)
        .exclude(school_class__branch_id__in=visible)
        .select_related("school_class__branch", "subject")
    ):
        held.setdefault(duty.school_class.branch.name, {}).setdefault(
            duty.school_class.name, [],
        ).append(duty.subject.name)
    for slot in (
        TimetableSlot.all_objects.filter(teacher_id=user.pk, **live)
        .exclude(session__status=SessionStatus.ARCHIVED)
        .filter(school_class__branch__isnull=False)
        .exclude(school_class__branch_id__in=visible)
        .select_related("school_class__branch")
    ):
        roles = held.setdefault(slot.school_class.branch.name, {}).setdefault(
            slot.school_class.name, [],
        )
        if "lessons" not in roles:
            roles.append("lessons")
    if not held:
        return

    branch = sorted(held)[0]
    classes = sorted(held[branch])
    raise TeacherStillHoldsClasses(
        f"{_full_name(fresh)} still teaches {_names(classes)} at {branch}. "
        f"Hand {'it' if len(classes) == 1 else 'them'} to someone there first.",
        teacher=_full_name(fresh), held=held,
    )


def assert_class_teachers_reach(tenant, school_class):
    """Refuse moving *school_class* to a branch its teachers do not work at.

    Its class teacher, its subject teachers and its timetabled lesson teachers
    each have to see the class at its new branch, or they lose it. Run after the
    move is saved, inside the same transaction, so raising undoes the move. A
    class made school-wide, and any class at a school with one branch, passes.
    """
    from schools.vs_calendar.models import TimetableSlot
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids_for
    from vs_tenants.models import Branch
    from vs_user.models import User

    from ..models import TeachingAssignment

    if school_class.branch_id is None or not _several_branches(tenant):
        return
    ids = set()
    if school_class.class_teacher_id is not None:
        ids.add(school_class.class_teacher.user_id)
    ids.update(
        TeachingAssignment.all_objects.filter(tenant=tenant, school_class=school_class)
        .values_list("staff__user_id", flat=True),
    )
    ids.update(
        TimetableSlot.all_objects.filter(
            tenant=tenant, school_class=school_class, teacher__isnull=False,
        ).values_list("teacher_id", flat=True),
    )
    people = list(User.objects.filter(pk__in=ids).order_by("first_name", "last_name"))
    reach = visible_branch_ids_for(people, tenant)
    outside = [
        _full_name(person) for person in people
        if reach.get(person.pk) is not WHOLE_TENANT
        and school_class.branch_id not in reach.get(person.pk, frozenset())
    ]
    if not outside:
        return
    branch = Branch.all_objects.get(pk=school_class.branch_id).name
    raise ClassTeachersNotAtBranch(
        f"{_names(outside)} {'does' if len(outside) == 1 else 'do'} not work at {branch}. "
        f"Give {'them' if len(outside) == 1 else 'each of them'} a role there first, "
        f"or hand {school_class.name} to someone who does.",
        teachers=outside, branch=branch,
    )


def current_lead(tenant, session, school_class, subject):
    """Whoever owns this pairing now, or None."""
    from ..models import TeachingAssignment

    return (
        TeachingAssignment.objects.filter(
            tenant=tenant, session=session, school_class=school_class,
            subject=subject, part=TeachingPart.LEAD,
        )
        .select_related("staff__user")
        .first()
    )


@transaction.atomic
def assign(*, tenant, staff, school_class, subject, session, part, actor):
    """Record that this person teaches this subject to this class.

    Refuses a second lead rather than displacing the first. A school promoting
    somebody over a colleague should do it deliberately, in two steps it can see,
    rather than discovering afterwards that the person who had the class no
    longer does.

    Refuses somebody who has left. A duty carries no end date of its own, so a
    resigned teacher given a subject would stay on it with nothing to say so.
    """
    from ..models import TeachingAssignment

    assert_session_open(session)
    if not staff.is_on_roll:
        raise StaffHasLeft(
            f"{_name(staff)} no longer works here, so they cannot be given "
            f"{subject.name} in {school_class.name}. Reinstate them first if "
            f"they are coming back.",
        )
    assert_teacher_reaches_class(tenant, staff.user, school_class)
    if part == TeachingPart.LEAD:
        held = current_lead(tenant, session, school_class, subject)
        if held is not None and held.staff_id != staff.pk:
            raise LeadAlreadySet(
                f"{_name(held.staff)} is already the main teacher for "
                f"{subject.name} in {school_class.name}. Move them to assisting "
                f"first, then make this person main.",
                current_lead=_name(held.staff),
            )

    assignment, created = TeachingAssignment.objects.get_or_create(
        tenant=tenant, session=session, school_class=school_class,
        subject=subject, staff=staff,
        defaults={"part": part, "assigned_by": actor},
    )
    if not created and assignment.part != part:
        return set_part(assignment, part, actor=actor)
    if created:
        audit.emit_teaching_assigned(assignment, actor=actor)
    return assignment


@transaction.atomic
def set_part(assignment, part, *, actor):
    """Make lead, or step back. The same refusal applies in both directions."""
    assert_session_open(assignment.session)
    if assignment.part == part:
        return assignment
    if part == TeachingPart.LEAD:
        held = current_lead(
            assignment.tenant, assignment.session, assignment.school_class,
            assignment.subject,
        )
        if held is not None and held.pk != assignment.pk:
            raise LeadAlreadySet(
                f"{_name(held.staff)} is already the main teacher for "
                f"{assignment.subject.name} in {assignment.school_class.name}. "
                f"Move them to assisting first, then make this person main.",
                current_lead=_name(held.staff),
            )
    assignment.part = part
    assignment.save(update_fields=["part", "updated_at"])
    audit.emit_teaching_assigned(assignment, actor=actor)
    return assignment


@transaction.atomic
def unassign(assignment, *, actor):
    """Remove somebody from a subject in a class.

    Never refused for leaving a gap: a school that has lost a teacher needs the
    record to say so, and FR-017's coverage is what surfaces the consequence.
    """
    assert_session_open(assignment.session)
    audit.emit_teaching_unassigned(assignment, actor=actor)
    assignment.delete()


@transaction.atomic
def set_class_teacher(school_class, staff, *, actor, tenant):
    """Designate, replace or clear the class teacher.

    Written to ``SchoolClass.class_teacher``, which is unique per class by
    construction. Replacing audits both the old designation and the new,
    because a trail that records only the arrival cannot say why the previous
    holder stopped appearing.

    Each change also closes the class's open :class:`~..models.ClassTeacherTerm`
    and opens the next, which is the class's history as a school reads it. The
    class row is locked first, so two administrators saving at once take turns
    rather than opening two terms.

    Refuses somebody who has left: the designation carries no end date, so a
    resigned teacher made class teacher would stay one with nothing to say so.
    """
    from schools.vs_academics.models import SchoolClass

    from ..models import ClassTeacherTerm

    locked = SchoolClass.all_objects.select_for_update().get(pk=school_class.pk)
    previous = locked.class_teacher_id
    if previous == getattr(staff, "pk", None):
        return school_class
    if staff is not None and not staff.is_on_roll:
        raise StaffHasLeft(
            f"{_name(staff)} no longer works here, so they cannot be class "
            f"teacher of {school_class.name}. Reinstate them first if they are "
            f"coming back.",
        )
    if staff is not None:
        assert_teacher_reaches_class(tenant, staff.user, school_class)

    now = timezone.now()
    actor_id = getattr(actor, "pk", None)
    ClassTeacherTerm.all_objects.filter(
        school_class_id=school_class.pk, ended_at__isnull=True,
    ).update(ended_at=now, ended_by_id=actor_id, updated_at=now)
    if staff is not None:
        ClassTeacherTerm.all_objects.create(
            tenant_id=school_class.tenant_id, school_class_id=school_class.pk,
            staff=staff, started_at=now, started_by_id=actor_id,
        )

    school_class.class_teacher = staff
    school_class.save(update_fields=["class_teacher", "updated_at"])
    audit.emit_class_teacher_set(school_class, previous, actor=actor, tenant=tenant)
    return school_class


def class_teacher_terms(school_class):
    """The class's terms, the one running now first and the oldest last.

    A term whose start is unknown predates every recorded one, so it sorts
    last; ties fall back to the order the terms were written.
    """
    from django.db.models import F

    from ..models import ClassTeacherTerm

    return (
        ClassTeacherTerm.all_objects.filter(school_class_id=school_class.pk)
        .select_related("staff__user", "started_by", "ended_by")
        .order_by(F("started_at").desc(nulls_last=True), "-id")
    )


def coverage(tenant, user, session):
    """Every subject a class should be taught, and who is teaching it.

    The universe is the school's own offerings: ``SubjectOffering`` pairs a
    subject with a level, and the classes at that level are what actually need
    covering. Crossing every class with every subject instead would report
    Primary 4 as missing a Physics teacher.

    Two kinds of gap, counted separately, because they are different problems. A
    **coverage gap** is a pairing nobody teaches. A **lead gap** is a pairing
    with assistants and no lead: it is being taught and nobody owns the marks.

    Returns ``(rows, coverage_gaps, lead_gaps)`` where each row is a class, a
    subject, its lead and its assistants. This is the one genuinely computable
    warning in the module, because it counts rows, and a screen may present it
    confidently.
    """
    from schools.vs_academics.models import SchoolClass, SubjectOffering

    from ..models import TeachingAssignment
    from .scoping import scope_staff  # noqa: F401  (kept for symmetry of imports)

    classes = list(
        _visible_classes(tenant, user, session)
        .select_related("level", "branch")
        .order_by("level__name", "name")
    )
    offerings = (
        SubjectOffering.objects.filter(
            tenant=tenant, level_id__in={row.level_id for row in classes},
        )
        .select_related("subject")
        .order_by("subject__name")
    )
    by_level: dict[int, list] = {}
    for offering in offerings:
        by_level.setdefault(offering.level_id, []).append(offering.subject)

    assignments = (
        TeachingAssignment.objects.filter(
            tenant=tenant, session=session,
            school_class_id__in=[row.pk for row in classes],
        )
        .select_related("staff__user", "subject", "school_class")
    )
    held: dict[tuple[int, int], list] = {}
    for row in assignments:
        held.setdefault((row.school_class_id, row.subject_id), []).append(row)

    rows, coverage_gaps, lead_gaps = [], 0, 0
    for school_class in classes:
        for subject in by_level.get(school_class.level_id, ()):
            team = held.get((school_class.pk, subject.pk), [])
            lead = next(
                (row for row in team if row.part == TeachingPart.LEAD), None,
            )
            assistants = [row for row in team if row.part == TeachingPart.ASSISTANT]
            if not team:
                coverage_gaps += 1
            elif lead is None:
                lead_gaps += 1
            rows.append({
                "school_class": school_class,
                "subject": subject,
                "lead": lead,
                "assistants": assistants,
            })
    return rows, coverage_gaps, lead_gaps


def duty_holders(tenant, session, pairs) -> dict:
    """Who holds a teaching duty for each ``(class id, subject id)`` in *pairs*.

    Answers ``{(class_id, subject_id): {user_id, ...}}``, with an empty set
    for a pair nobody teaches. Lead and assistant both count: each is a duty
    to teach the subject to the class.

    The lookup other modules make rather than reading ``TeachingAssignment``
    themselves, so that "has a duty" keeps one meaning across the platform.
    One query, however many pairs are asked about.
    """
    from django.db.models import Q

    from ..models import TeachingAssignment

    pairs = {(int(c), int(s)) for c, s in pairs if c and s}
    out = {pair: set() for pair in pairs}
    if not pairs or session is None:
        return out
    wanted = Q()
    for class_id, subject_id in pairs:
        wanted |= Q(school_class_id=class_id, subject_id=subject_id)
    rows = (
        TeachingAssignment.objects.filter(tenant=tenant, session=session)
        .filter(wanted)
        .values_list("school_class_id", "subject_id", "staff__user_id")
    )
    for class_id, subject_id, user_id in rows:
        out[(class_id, subject_id)].add(user_id)
    return out


def _visible_classes(tenant, user, session):
    """Classes this caller may see, inclusive of the school-wide ones.

    A shared class has a null branch and belongs to every branch, so a
    branch-bound caller must still see it. This is ``vs_academics``' rule and is
    read through rather than reimplemented.
    """
    from django.db.models import Q

    from schools.vs_academics.models import SchoolClass
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    queryset = SchoolClass.objects.filter(tenant=tenant, session=session)
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return queryset
    scope = Q(branch__isnull=True)
    if visible:
        scope |= Q(branch_id__in=tuple(sorted(visible)))
    return queryset.filter(scope)


def _name(staff) -> str:
    user = getattr(staff, "user", None)
    if user is None:
        return "Somebody"
    full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return full or "Somebody"
