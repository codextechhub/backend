"""What the parent portal reads, for the parent already admitted.

Every function here takes the guardian record the request was admitted as
(``permissions.linked_guardian``) and children resolved from it
(``permissions.child_of``), never an id taken straight from the request, and
reads from the module that owns the fact. Nothing is stored here. The
timetable, attendance and results of a child are the pupil portal's own
functions, called with the child's record.

**A parent holds no role, so Field Access switches do not apply.** Field
Access belongs to roles; what the portal shows a parent is a fixed, narrow
shape of their own and their children's records, written for them. A child's
record carries no staff note and no medical note, and the other guardians on
it appear by name and relationship only, never by phone, email or address.
"""
from __future__ import annotations

from schools.vs_student_portal.services import active_enrolment


def _initials(first: str, last: str, full_name: str) -> str:
    parts = [p for p in (first, last) if p] or full_name.split()[:2]
    return "".join(p[0] for p in parts).upper()


def child_enrolments(children) -> dict:
    """Each child's class, keyed by child id, in one query for any number of children.

    The same choice as the pupil portal's ``active_enrolment``: the placement in
    the running year, else the one in the year being set up.
    """
    from schools.vs_students.models import ClassEnrolment

    chosen: dict = {}
    rows = (
        ClassEnrolment.objects.filter(student__in=[c.pk for c in children], is_active=True)
        .select_related("school_class__level", "session")
        .order_by("session__start_date")
    )
    for row in rows:
        if row.session.status == "ACTIVE":
            chosen[row.student_id] = row
        elif row.session.status == "DRAFT":
            chosen.setdefault(row.student_id, row)
    return chosen


def _child_summary(child, enrolment, *, request, show_branch: bool) -> dict:
    from schools.vs_students.services.documents import face_url

    data = {
        "id": child.pk,
        "first_name": child.first_name,
        "full_name": child.full_name,
        "initials": _initials(child.first_name, child.last_name, child.full_name),
        "photo_url": face_url(child, request=request) or "",
        "admission_number": child.student_number or "",
        "class_name": enrolment.school_class.name if enrolment else "",
        "relationship": child.parent_relationship,
    }
    if show_branch:
        data["branch"] = {"id": child.branch_id, "name": child.branch.name}
    return data


def me(guardian, children, *, request) -> dict:
    """The parent, their school, the year and term, and each of their children."""
    from schools.vs_academics.models import AcademicSession
    from schools.vs_gradebook.services.resolve import current_term
    from schools.vs_students.services.scoping import branch_dimension_applies
    from vs_config.clock import branch_today
    from vs_user.serializers import school_public_info

    tenant = guardian.tenant
    show_branch = branch_dimension_applies(tenant)
    school = school_public_info(getattr(tenant, "school_profile", None), request) or {}
    session = (
        AcademicSession.objects.filter(tenant=tenant, status="ACTIVE")
        .order_by("-start_date", "-pk").first()
    )
    term = current_term(tenant, branch_today(tenant, None))
    placed = child_enrolments(children)
    return {
        "parent": {
            "first_name": guardian.first_name or guardian.full_name.split(" ")[0],
            "full_name": guardian.full_name,
            "initials": _initials(guardian.first_name, guardian.last_name, guardian.full_name),
        },
        "school": {"name": school.get("name", ""), "logo_url": school.get("logo") or ""},
        "show_branch": show_branch,
        "session": {"id": session.pk, "name": session.name} if session is not None else None,
        "term": {"id": term.pk, "name": term.name} if term is not None else None,
        "children": [
            _child_summary(c, placed.get(c.pk), request=request, show_branch=show_branch)
            for c in children
        ],
    }


def child_record(guardian, child, *, request) -> dict:
    """One child's record as a parent may see it.

    The other guardians on the record are named with their relationship and
    nothing else.
    """
    from schools.vs_students.models import StudentGuardian
    from schools.vs_students.services.scoping import branch_dimension_applies

    enrolment = active_enrolment(child)
    show_branch = branch_dimension_applies(child.tenant)
    data = _child_summary(child, enrolment, request=request, show_branch=show_branch)
    data.update({
        "date_of_birth": child.date_of_birth,
        "level_name": (
            enrolment.school_class.level.name
            if enrolment and enrolment.school_class.level_id else ""
        ),
        "session": (
            {"id": enrolment.session.pk, "name": enrolment.session.name} if enrolment else None
        ),
        "class_is_next_year": bool(enrolment and enrolment.session.status != "ACTIVE"),
        "other_guardians": [
            {"name": link.guardian.full_name, "relationship": link.relationship_label}
            for link in StudentGuardian.objects.filter(student=child)
            .exclude(guardian=guardian).select_related("guardian")
        ],
    })
    return data
