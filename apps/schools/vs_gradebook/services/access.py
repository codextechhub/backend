"""Which class, subject and sheet a caller may open, and what they may do on it.

**Opening.** A class of another school is a 404, as is a class in a branch the
caller cannot see, so identifiers cannot be enumerated. A class shared across
the school (no branch) belongs to the school's only branch where there is one;
at a school with several it has no branch to keep results in
(:class:`~..exceptions.ClassHasNoBranch`). A subject the class's level does not
offer is a 404, and so is an optional subject nobody takes in that class and
term: there is no sheet for it.

**Reading.** The key to open sheets at all is the view's to check. Which sheet
is decided here. A holder of ``hub.view`` or ``sheet.enter_any`` reads every
sheet in their branches. Anyone else reads the sheets they have a part in: the
main teacher, an assistant, the class teacher, the head of the subject's
department.

**Entering.** The main teacher enters, with ``sheet.enter``. The holder of
``sheet.enter_any`` enters any sheet they can open.

**Moderating.** The head of the subject's department decides; the key is
``moderation.approve`` and the department is the rule.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import SchoolClass, Subject, SubjectOffering
from schools.vs_academics.services import electives, heads
from schools.vs_students.services.scoping import scope_classes
from vs_rbac.evaluator import has_permission

from ..constants import (
    PERM_CLASS_PUBLISH,
    PERM_HUB_VIEW,
    PERM_SHEET_ENTER,
    PERM_SHEET_ENTER_ANY,
)
from ..exceptions import (
    ClassHasNoBranch,
    InvalidTerm,
    NotClassTeacher,
    NotHeadOfDepartment,
    NotMainTeacher,
    NotYourClass,
    NotYourSheet,
)
from . import resolve

NO_SUCH_CLASS = "No such class at this school."
NO_SUCH_SHEET = "No such score sheet at this school."


def open_class(tenant, user, class_id):
    """``(class, branch)`` for a class the caller may open.

    Raises 404 for a class that is not this school's or sits in a branch the
    caller cannot see, and :class:`ClassHasNoBranch` for a shared class at a
    school with several branches.
    """
    school_class = (
        scope_classes(SchoolClass.objects.filter(tenant=tenant, pk=class_id), user, tenant)
        .select_related(
            "session", "level", "level__program", "branch", "class_teacher",
            "class_teacher__user",
        )
        .first()
    )
    if school_class is None:
        raise NotFound(NO_SUCH_CLASS)
    branch = resolve.class_branch(tenant, school_class)
    if branch is None:
        raise ClassHasNoBranch()
    return school_class, branch


def assert_term_of_class(school_class, term) -> None:
    """Refuse a term from a school year other than the class's own."""
    if term.session_id != school_class.session_id:
        raise InvalidTerm(
            f"{term.name} is not a term of {school_class.session.name}, the year "
            f"{school_class.name} runs in. Choose one of that year's terms.",
        )


def open_subject(tenant, school_class, term, subject_id):
    """``(subject, is_elective, taker_ids)`` for a subject the class has a sheet for, else 404.

    ``taker_ids`` is the set of students recorded as taking an optional subject,
    and None for a core one.
    """
    subject = (
        Subject.objects.filter(tenant=tenant, pk=subject_id)
        .select_related("department", "department__head", "department__head__user")
        .first()
    )
    offering = (
        SubjectOffering.objects.filter(
            tenant=tenant, level_id=school_class.level_id, subject=subject,
        ).select_related("subject").first()
        if subject is not None else None
    )
    if offering is None:
        raise NotFound(NO_SUCH_SHEET)
    if electives.effective_is_core(offering):
        return subject, False, None
    takers = electives.takers(tenant, school_class, term).get(subject.pk, set())
    if not takers:
        raise NotFound(NO_SUCH_SHEET)
    return subject, True, takers


# ── What a caller may do on a sheet ────────────────────────────────────────

def _is_user(staff, user) -> bool:
    return staff is not None and staff.user_id == user.pk


def is_main_teacher(user, school_class, subject) -> bool:
    """Whether *user* leads *subject* in *school_class* for the class's year."""
    return _is_user(resolve.main_teacher(school_class, subject, school_class.session_id), user)


def heads_department(user, tenant, subject) -> bool:
    """Whether *user* heads the department *subject* belongs to."""
    if subject.department_id is None:
        return False
    return heads.departments_headed_by(user, tenant).filter(pk=subject.department_id).exists()


def may_read(user, tenant, school_class, subject) -> bool:
    """Whether *user* may open the sheet of *subject* in *school_class*."""
    if has_permission(user, PERM_HUB_VIEW, tenant=tenant):
        return True
    if has_permission(user, PERM_SHEET_ENTER_ANY, tenant=tenant):
        return True
    if _is_user(school_class.class_teacher, user):
        return True
    if is_main_teacher(user, school_class, subject):
        return True
    if any(
        _is_user(staff, user)
        for staff in resolve.assistants(school_class, subject, school_class.session_id)
    ):
        return True
    return heads_department(user, tenant, subject)


def assert_may_read(user, tenant, school_class, subject) -> None:
    if not may_read(user, tenant, school_class, subject):
        raise NotYourSheet()


def may_enter(user, tenant, school_class, subject) -> bool:
    """Whether *user* may put scores on the sheet: its main teacher, or an ``enter_any`` holder."""
    if has_permission(user, PERM_SHEET_ENTER_ANY, tenant=tenant):
        return True
    return is_main_teacher(user, school_class, subject) and has_permission(
        user, PERM_SHEET_ENTER, tenant=tenant,
    )


def assert_may_enter(user, tenant, school_class, subject) -> None:
    if not may_enter(user, tenant, school_class, subject):
        raise NotMainTeacher()


def assert_heads_department(user, tenant, subject) -> None:
    if not heads_department(user, tenant, subject):
        raise NotHeadOfDepartment()


# ── What a caller may do on a class ────────────────────────────────────────

def is_class_teacher(user, school_class) -> bool:
    """Whether *user* is the staff member named as *school_class*'s class teacher."""
    return _is_user(school_class.class_teacher, user)


def oversees_results(user, tenant) -> bool:
    """Whether *user* holds a key that reads every class's results in their branches."""
    return has_permission(user, PERM_HUB_VIEW, tenant=tenant) or has_permission(
        user, PERM_CLASS_PUBLISH, tenant=tenant,
    )


def assert_may_read_class(user, tenant, school_class) -> None:
    """The class teacher, or a holder of ``hub.view`` or ``class.publish``, may open the broadsheet."""
    if not (is_class_teacher(user, school_class) or oversees_results(user, tenant)):
        raise NotYourClass()


def assert_class_teacher(user, school_class) -> None:
    """Only the class teacher compiles and submits the class."""
    if not is_class_teacher(user, school_class):
        raise NotClassTeacher()


def assert_may_read_compile(user, tenant, school_class) -> None:
    """The class teacher, or a holder of ``class.publish``, may read a student's compile."""
    if not (
        is_class_teacher(user, school_class)
        or has_permission(user, PERM_CLASS_PUBLISH, tenant=tenant)
    ):
        raise NotClassTeacher()

