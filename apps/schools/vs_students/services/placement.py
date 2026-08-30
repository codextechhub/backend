"""Putting a student in a class, and moving them between classes.

One function does both, because they are the same act with one difference: a
transfer has a previous placement to close and a reason to record, and a first
placement has neither. Writing them apart is how the two drift until only one
of them checks capacity.

FRD M11 v2.4 FR-006.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import EnrolmentOutcome, StudentStatus
from ..exceptions import (
    ClassAtCapacity,
    NoActiveSession,
    ReasonRequired,
)
from ..models import ClassEnrolment
from .scoping import assert_class_reachable, scope_classes


def active_session(tenant):
    """The school year a placement lands in. There is only ever one.

    Naming another is refused rather than honoured: a placement in a year that
    has not started or has ended is a roster nobody will look at and a fee
    nobody will raise.
    """
    from schools.vs_academics.models import AcademicSession, SessionStatus

    found = AcademicSession.objects.filter(
        tenant=tenant, status=SessionStatus.ACTIVE,
    ).first()
    if found is None:
        raise NoActiveSession()
    return found


def resolve_class(tenant, user, class_id):
    """A class this caller can see, or 404.

    404 and not 403: a class the caller cannot see does not exist as far as
    they are concerned, and telling them it exists elsewhere is the same leak
    as telling them a student does.
    """
    from schools.vs_academics.models import SchoolClass

    row = scope_classes(
        SchoolClass.objects.filter(tenant=tenant, is_active=True), user, tenant,
    ).select_related("branch", "level").filter(pk=class_id).first()
    if row is None:
        raise NotFound("No such class at this school.")
    return row


def seats_used(school_class, session) -> int:
    return ClassEnrolment.objects.filter(
        school_class=school_class, session=session, is_active=True,
    ).count()


def capacity_state(school_class, session, *, adding=1):
    """(used, capacity, would_overflow). A null capacity means unlimited."""
    used = seats_used(school_class, session)
    cap = school_class.capacity
    if cap is None:
        return used, None, False
    return used, cap, (used + adding) > cap


def assert_capacity(school_class, session, *, adding=1, acknowledged=False):
    """Refuse a full class unless the caller has said they mean it.

    The acknowledgement needs no extra permission key. The design shows the
    seat count and the warning to whoever is doing the enrolling and then lets
    them proceed; reserving the override to school.students.manage would stop
    the screen working for the registrar it was drawn for. It is audited either
    way, which is the control that actually matters.
    """
    used, cap, over = capacity_state(school_class, session, adding=adding)
    if over and not acknowledged:
        raise ClassAtCapacity(
            f"{school_class.name} holds {used} of {cap} seats. Adding "
            f"{adding} would put it over capacity. You can go ahead anyway.",
            school_class=school_class.pk, capacity=cap, used=used,
        )
    return used, cap, over


@transaction.atomic
def place(
    student, school_class, *, actor, session=None, reason="", effective_date=None,
    allow_over_capacity=False,
):
    """Place or move *student*, closing any previous placement in one go.

    Returns ``(enrolment, was_transfer, over_capacity)``.
    """
    session = session or active_session(student.tenant)
    assert_class_reachable(student.branch, school_class)

    previous = (
        student.enrolments.filter(session=session, is_active=True)
        .select_related("school_class").first()
    )
    is_transfer = previous is not None and previous.school_class_id != school_class.pk

    if previous is not None and previous.school_class_id == school_class.pk:
        # Already there. Not an error - a school clicking twice has not done
        # anything wrong - but nothing to write either.
        return previous, False, False

    if is_transfer and not (reason or "").strip():
        raise ReasonRequired(
            "Say why this student is moving class. It goes into their history.",
        )

    _, _, over = assert_capacity(
        school_class, session, adding=1, acknowledged=allow_over_capacity,
    )

    if previous is not None:
        previous.is_active = False
        previous.ended_at = timezone.now()
        previous.outcome = EnrolmentOutcome.ENDED
        previous.save(update_fields=["is_active", "ended_at", "outcome", "updated_at"])

    enrolment = ClassEnrolment.objects.create(
        tenant=student.tenant, student=student, school_class=school_class,
        session=session, is_active=True,
        effective_date=effective_date or timezone.localdate(),
        reason=(reason or "") if is_transfer else "",
        outcome=EnrolmentOutcome.CURRENT,
        assigned_by=actor,
    )

    # A placement is what carries an ENROLLED student the rest of the way to
    # ACTIVE. Done here rather than by the caller so no route can place a
    # student and leave them off the register.
    if student.status == StudentStatus.ENROLLED:
        from .status import transition

        transition(
            student, StudentStatus.ACTIVE, actor=actor,
            reason=f"Placed in {school_class.name}.",
            effective_date=enrolment.effective_date,
        )

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=(
            AuditActionType.STUDENT_CLASS_TRANSFERRED if is_transfer
            else AuditActionType.STUDENT_CLASS_ASSIGNED
        ),
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=(
            f"{student.full_name} moved from "
            f"{previous.school_class.name} to {school_class.name}."
            if is_transfer
            else f"{student.full_name} assigned to {school_class.name}."
        ),
        metadata={
            "class": school_class.name, "session": str(session),
            "reason": reason, "over_capacity": over,
            "effective_date": str(enrolment.effective_date),
        },
    )
    return enrolment, is_transfer, over


def roster(tenant, user, school_class, session):
    """Who is in this class, narrowed by the caller's branches.

    The narrowing matters most on a **school-wide** class, which has no branch
    of its own and holds children from several. A branch-bound viewer sees
    their own branches' children in it and no others; a school-level viewer
    sees all of them. That is the one place a row is visible while part of its
    content is not, and it follows from a shared class holding branch-bound
    children.
    """
    from .scoping import scope_students
    from ..models import Student

    qs = Student.objects.filter(
        tenant=tenant,
        enrolments__school_class=school_class,
        enrolments__session=session,
        enrolments__is_active=True,
    )
    return scope_students(qs, user, tenant).distinct()


def fullest_classes(tenant, user, session, *, limit=3, branch=None):
    """The classes nearest their capacity, for the directory's capacity panel.

    Computed with one aggregate rather than by counting each class's roster,
    because a school with sixty classes would otherwise cost sixty queries to
    draw one card.

    *branch* narrows to classes at that site PLUS the school-wide ones, which
    is what a class with no branch means. Without it the panel warned a Main
    Branch registrar about a full class at the Annex, which is neither hers to
    fill nor hers to fix.
    """
    from django.db.models import Q as _Q

    from schools.vs_academics.models import SchoolClass

    qs = scope_classes(
        SchoolClass.objects.filter(
            tenant=tenant, is_active=True, capacity__isnull=False,
        ),
        user, tenant,
    )
    if branch is not None:
        qs = qs.filter(_Q(branch=branch) | _Q(branch__isnull=True))
    qs = qs.annotate(
        used=Count(
            "enrolments",
            filter=Q(enrolments__session=session, enrolments__is_active=True),
        ),
    )
    rows = [
        {
            "id": c.pk, "name": c.name, "used": c.used, "capacity": c.capacity,
            "remaining": c.capacity - c.used,
        }
        for c in qs
    ]
    rows.sort(key=lambda r: (r["remaining"], -r["used"]))
    return [r for r in rows if r["remaining"] <= 5][:limit]
