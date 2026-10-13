"""The two results hubs: the school's, and a teacher's own.

**The administrator's hub** answers, for one term, "how are the results coming
and who needs chasing". It is assembled in a fixed number of queries however
many classes the school has (:mod:`.book`).

*Scope.* A branch-bound caller sees their own branches' classes and a
whole-school caller sees every class; ``branch`` narrows either to one visible
branch. A class with no branch at a school that has several has nowhere for
results to live (:class:`~..exceptions.ClassHasNoBranch`), so it is not on the
lists; a whole-school reader looking at everything is told which they are, so the
gap can be fixed rather than silently read as "all in". At a school with one
branch no row carries a branch.

*The counts.* ``sheets_in`` of ``sheets_due`` are the sheets the classes owe and
the ones that are in. A class is compiled once it is awaiting the principal or
published. ``held`` is the open fee holds on students on those classes' rolls.

*Late sheets.* Every due sheet not yet in, with the one reason the platform
knows, looked for in this order: it is waiting for its department head; it was
returned, and by whom; nobody is assigned as main teacher; the teacher's last
working day; the teacher cannot sign in (only for a reader who may unlock staff
accounts, who is also given the staff record to open; everyone else falls
through to the next); and otherwise how many of the roll are entered.

**The teacher's hub** lists the sheets the caller is the main teacher of, with
entered of roll, the examination paper, where the sheet stands and what to do
next, and their other duties: the classes they lead as class teacher and the
moderation queue of each department they head.
"""
from __future__ import annotations

from django.db.models import Count, Q

from schools.vs_academics.models import SchoolClass
from schools.vs_academics.services import heads
from schools.vs_attendance.services.scope import placed_classes, school_branches
from schools.vs_calendar.services.calendar import results_day
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import TeachingAssignment
from schools.vs_students.services.scoping import scope_classes
from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_rbac.scoping import WHOLE_TENANT, branch_q_for_user, visible_branch_ids

from ..constants import (
    CTA_CONTINUE,
    CTA_ENTER,
    CTA_REVISE,
    CTA_VIEW,
    DUTY_CLASS_TEACHER,
    DUTY_HEAD_OF_DEPARTMENT,
    REASON_AWAITING_MODERATION,
    REASON_CANNOT_SIGN_IN,
    REASON_ENTERED,
    REASON_LAST_WORKING_DAY,
    REASON_NO_MAIN_TEACHER,
    REASON_RETURNED,
    ClassStage,
    SheetStatus,
)
from ..models import ResultHold, ScoreSheet, SheetReminder
from . import book

CLASS_RELATIONS = (
    "session", "level", "level__program", "branch", "class_teacher", "class_teacher__user",
)


def _person(staff) -> dict | None:
    return {"id": staff.pk, "name": staff.user.full_name} if staff is not None else None


def reachable_classes(tenant, user, term, branch, **extra):
    """``(placed, unplaced, branches, visible)`` for the term's active classes the caller can see."""
    branches = school_branches(tenant)
    visible = visible_branch_ids(user, tenant)
    classes = list(
        scope_classes(
            SchoolClass.objects.filter(
                tenant=tenant, is_active=True, session_id=term.session_id, **extra,
            ),
            user, tenant,
        ).select_related(*CLASS_RELATIONS)
    )
    placed, unplaced = placed_classes(classes, branches, visible, branch)
    placed.sort(key=lambda pair: (pair[0].level.order_index, pair[0].level_id, pair[0].name))
    return placed, unplaced, branches, visible


# ── The administrator's hub ────────────────────────────────────────────────

def late_reason(item, entered: int, *, show_locked_accounts: bool, tenant, branch) -> tuple:
    """``(kind, sentence)`` for a due sheet that is not in. See the module docstring for the order."""
    sheet = item.sheet
    teacher = item.teacher
    if sheet is not None and sheet.status == SheetStatus.SUBMITTED:
        head = item.head.user.full_name if item.head is not None else "the head of department"
        return REASON_AWAITING_MODERATION, f"Waiting for {head} to moderate it"
    if sheet is not None and sheet.status == SheetStatus.RETURNED:
        by = sheet.returned_by.full_name if sheet.returned_by is not None else None
        return REASON_RETURNED, f"Returned by {by}" if by else "Returned for correction"
    if teacher is None:
        return REASON_NO_MAIN_TEACHER, "No main teacher assigned"
    if teacher.exit_date is not None:
        return (
            REASON_LAST_WORKING_DAY,
            f"Last working day {format_date(teacher.exit_date, tenant, branch=branch)}",
        )
    if show_locked_accounts and not teacher.user.may_sign_in:
        return REASON_CANNOT_SIGN_IN, f"{teacher.user.full_name} cannot sign in"
    return REASON_ENTERED, f"{entered} of {len(item.roll_ids)} entered"


def admin_hub(tenant, user, term, branch, *, show_locked_accounts: bool) -> dict:
    """The results hub for *term*, before Field Access is applied."""
    placed, unplaced, branches, visible = reachable_classes(tenant, user, term, branch)
    several = len(branches) > 1
    entries = book.load(tenant, term, placed)
    entered = book.entered_counts(tenant, entries)

    on_rolls = {student_id for entry in entries for student_id in entry.roll_ids}
    held = sum(
        1 for student_id in ResultHold.objects.filter(
            tenant=tenant, term=term, lifted_at__isnull=True,
        ).values_list("student_id", flat=True)
        if student_id in on_rolls
    )

    today = {home.pk: branch_today(tenant, home) for _, home in placed}
    late_sheets = [
        item.sheet.pk
        for entry in entries for item in entry.due
        if item.sheet is not None and not item.is_in
    ]
    reminded = set(
        SheetReminder.objects.filter(
            tenant=tenant, sheet_id__in=late_sheets, on_date__in=set(today.values()),
        ).values_list("sheet_id", "on_date")
    )

    classes, late = [], []
    for entry in entries:
        school_class, home = entry.school_class, entry.home
        classes.append({
            "id": school_class.pk, "name": school_class.name,
            "branch": {"id": home.pk, "name": home.name} if several else None,
            "program": {
                "id": school_class.level.program_id, "name": school_class.level.program.name,
            },
            "subjects_in": entry.subjects_in, "subjects_due": entry.subjects_due,
            "stage": entry.stage, "returned": entry.returned,
            "class_teacher": _person(school_class.class_teacher),
        })
        for item in entry.due:
            if item.is_in:
                continue
            kind, sentence = late_reason(
                item, entered[(school_class.pk, item.subject.pk)],
                show_locked_accounts=show_locked_accounts, tenant=tenant, branch=home,
            )
            late.append({
                "class": {"id": school_class.pk, "name": school_class.name},
                "subject": {"id": item.subject.pk, "name": item.subject.name},
                "main_teacher": _person(item.teacher),
                "status": item.status,
                "reason_kind": kind, "reason": sentence,
                "entered": entered[(school_class.pk, item.subject.pk)],
                "roll": len(item.roll_ids),
                "reminded_today": (
                    item.sheet is not None
                    and (item.sheet.pk, today[home.pk]) in reminded
                ),
                "staff_link": (
                    item.teacher.pk if kind == REASON_CANNOT_SIGN_IN else None
                ),
            })

    stages = [entry.stage for entry in entries]
    only = branch
    if only is None and visible is not WHOLE_TENANT and len(visible) == 1:
        only = next((b for b in branches if b.pk in visible), None)
    return {
        "branch_dimension": several,
        "term": {"id": term.pk, "name": term.name, "session": term.session.name},
        "results_day": results_day(tenant, term, only),
        "term_ends": term.end_date,
        "kpis": {
            "sheets_in": sum(entry.subjects_in for entry in entries),
            "sheets_due": sum(entry.subjects_due for entry in entries),
            "classes_compiled": sum(
                1 for stage in stages if stage in (ClassStage.AWAITING, ClassStage.PUBLISHED)
            ),
            "awaiting_approval": sum(1 for stage in stages if stage == ClassStage.AWAITING),
            "published": sum(1 for stage in stages if stage == ClassStage.PUBLISHED),
            "held": held,
        },
        "classes": classes,
        "late": late,
        "classes_without_branch": (
            [{"id": c.pk, "name": c.name} for c in unplaced]
            if visible is WHOLE_TENANT and branch is None else []
        ),
    }


# ── The teacher's hub ──────────────────────────────────────────────────────

def _cta(status: str) -> str:
    if status == SheetStatus.NOT_STARTED:
        return CTA_ENTER
    if status == SheetStatus.IN_PROGRESS:
        return CTA_CONTINUE
    if status == SheetStatus.RETURNED:
        return CTA_REVISE
    return CTA_VIEW


def teacher_hub(tenant, user, term) -> dict:
    """The caller's own sheets and duties for *term*, before Field Access is applied."""
    taught = set(
        TeachingAssignment.objects.filter(
            tenant=tenant, session_id=term.session_id, part=TeachingPart.LEAD,
            staff__user=user,
        ).values_list("school_class_id", flat=True)
    )
    placed = _my_classes(tenant, user, term, taught)
    entries = book.load(tenant, term, placed)
    entered = book.entered_counts(tenant, entries)

    mine = [
        (entry, item) for entry in entries for item in entry.due
        if item.teacher is not None and item.teacher.user_id == user.pk
    ]
    papers = book.exam_papers(
        tenant, term, {entry.school_class.pk for entry, _ in mine},
        {item.subject.pk for _, item in mine},
    )
    sheets = []
    for entry, item in mine:
        key = (entry.school_class.pk, item.subject.pk)
        sheets.append({
            "class": {"id": entry.school_class.pk, "name": entry.school_class.name},
            "subject": {"id": item.subject.pk, "name": item.subject.name},
            "entered": len(item.roll_ids) if item.is_in else entered[key],
            "roll": len(item.roll_ids),
            "exam_paper": papers.get(key),
            "status": item.status,
            "cta": _cta(item.status),
        })

    duties = [
        {
            "kind": DUTY_CLASS_TEACHER,
            "class": {"id": entry.school_class.pk, "name": entry.school_class.name},
            "department": None, "waiting": None,
            "subjects_in": entry.subjects_in, "subjects_due": entry.subjects_due,
            "stage": entry.stage,
        }
        for entry in entries
        if entry.school_class.class_teacher is not None
        and entry.school_class.class_teacher.user_id == user.pk
    ]
    headed = {department.pk: department for department in heads.departments_headed_by(user, tenant)}
    if headed:
        counts = (
            ScoreSheet.objects.filter(
                branch_q_for_user(user, "class_term__"),
                tenant=tenant, class_term__term=term, needs_moderation=True,
                subject__department_id__in=list(headed),
            )
            .values("subject__department_id")
            .annotate(waiting=Count("id", filter=Q(status=SheetStatus.SUBMITTED)))
        )
        for row in sorted(counts, key=lambda r: headed[r["subject__department_id"]].name):
            department = headed[row["subject__department_id"]]
            duties.append({
                "kind": DUTY_HEAD_OF_DEPARTMENT, "class": None,
                "department": {"id": department.pk, "name": department.name},
                "waiting": row["waiting"],
                "subjects_in": None, "subjects_due": None, "stage": None,
            })
    return {
        "term": {"id": term.pk, "name": term.name, "session": term.session.name},
        "sheets": sheets, "duties": duties,
    }


def _my_classes(tenant, user, term, taught_ids):
    """``[(class, branch)]`` for the classes the caller leads or teaches a subject of."""
    branches = school_branches(tenant)
    visible = visible_branch_ids(user, tenant)
    classes = list(
        scope_classes(
            SchoolClass.objects.filter(
                Q(pk__in=list(taught_ids)) | Q(class_teacher__user=user),
                tenant=tenant, is_active=True, session_id=term.session_id,
            ),
            user, tenant,
        ).select_related(*CLASS_RELATIONS)
    )
    placed, _ = placed_classes(classes, branches, visible)
    placed.sort(key=lambda pair: (pair[0].level.order_index, pair[0].level_id, pair[0].name))
    return placed
