"""Who is current staff, who holds a messaging key, and who can be reached.

**Current staff** is an active account of the school with a ``StaffProfile``
whose employment has not ended (``StaffProfile.is_on_roll``). Every reach
question below starts from that set, and it is asked as a subquery rather than
a list, so a school of any size costs the same number of queries.

**Staff reach** (staff to staff, group members, broadcast recipients): current
staff who hold ``school.messages.view`` and whose posting is inside the
caller's branch reach, or is school-wide. A caller with a whole-school reach
sees everyone. Holders of a key are found in bulk through
:func:`vs_rbac.evaluator.resolve_users_with_permission`, never by checking each
candidate.

**Family reach** (guardians and pupils with staff): a child's class teacher
this year, the teachers with a teaching assignment for the child's class this
year (the school setting ``messages.family_reach_subject_teachers``), and the
school office, meaning staff holding ``school.family_messages.office`` whose
branch reach covers the child's branch. Every one of them must also hold
``school.family_messages.send`` and ``school.messages.view`` and be current
staff. The same rule read the other way says which children a member of staff
may start a conversation about (:func:`family_children`).
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef, Prefetch, Q, Subquery

from ..constants import PERM_FAMILY_OFFICE, PERM_FAMILY_SEND, PERM_VIEW
from .config import MessagingSettings


def current_staff_profiles(tenant):
    """``StaffProfile`` rows of people who are on the roll with a working account."""
    from schools.vs_staff.constants import OFF_ROLL_STATUSES
    from schools.vs_staff.models import StaffProfile

    return (
        StaffProfile.objects.filter(tenant=tenant, user__is_active=True, user__tenant=tenant)
        .exclude(employment_status__in=OFF_ROLL_STATUSES)
    )


def current_staff_user_ids(tenant):
    """The ids of current staff, as a subquery."""
    return current_staff_profiles(tenant).values("user_id")


def is_current_staff(tenant, user) -> bool:
    return current_staff_profiles(tenant).filter(user_id=user.pk).exists()


def key_holders(tenant, key):
    """Active accounts of *tenant* holding permission *key* in any branch."""
    from vs_rbac.evaluator import ANY_BRANCH, resolve_users_with_permission

    return resolve_users_with_permission(tenant, ANY_BRANCH, key)


def staff_label(profile) -> str:
    """A member of staff as a contact is described: job title, else "Staff"."""
    return (profile.job_title or "").strip() or "Staff"


# ── Staff to staff ────────────────────────────────────────────────────────


def staff_contacts(tenant, caller, *, search: str = "", branch_id=None,
                   narrow_branch_id=None, name_search: bool = True):
    """The staff *caller* may start a conversation with, add to a group or address.

    ``branch_id`` filters by a posting the caller picked. ``narrow_branch_id``
    is the branch of the group or broadcast being built: its members are staff
    posted there or school-wide. ``name_search`` is False when the caller may
    not read people's names (Field Access), so a search term cannot be used to
    probe them.
    """
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    profiles = (
        current_staff_profiles(tenant)
        .filter(user_id__in=key_holders(tenant, PERM_VIEW).values("pk"))
        .exclude(user_id=caller.pk)
        .select_related("user", "branch")
    )
    reach = visible_branch_ids(caller, tenant)
    if reach is not WHOLE_TENANT:
        profiles = profiles.filter(Q(branch__isnull=True) | Q(branch_id__in=tuple(reach or ())))
    if narrow_branch_id is not None:
        profiles = profiles.filter(Q(branch__isnull=True) | Q(branch_id=narrow_branch_id))
    if branch_id is not None:
        profiles = profiles.filter(branch_id=branch_id)
    term = (search or "").strip()
    if term and name_search:
        profiles = profiles.filter(
            Q(user__first_name__icontains=term) | Q(user__last_name__icontains=term)
            | Q(job_title__icontains=term)
        )
    elif term:
        profiles = profiles.none()
    return profiles.order_by("user__first_name", "user__last_name", "pk")


def contactable_staff_ids(tenant, caller, ids, *, narrow_branch_id=None) -> set:
    """Which of the user ids *ids* are staff *caller* may address.

    An id that is out of reach and an id that does not exist are both simply
    absent from the answer.
    """
    wanted = {int(i) for i in ids}
    if not wanted:
        return set()
    found = staff_contacts(tenant, caller, narrow_branch_id=narrow_branch_id).filter(
        user_id__in=wanted,
    ).values_list("user_id", flat=True)
    return set(found)


# ── Families and staff ────────────────────────────────────────────────────


def office_reaches(tenant) -> dict:
    """``{user_id: WHOLE_TENANT | frozenset of branch ids}`` for office holders."""
    from vs_rbac.scoping import visible_branch_ids_for

    return visible_branch_ids_for(list(key_holders(tenant, PERM_FAMILY_OFFICE)), tenant)


def office_users_covering(tenant, branch_id) -> set:
    """Ids of the staff acting as the school office for *branch_id*."""
    from vs_rbac.scoping import WHOLE_TENANT

    return {
        user_id for user_id, reach in office_reaches(tenant).items()
        if reach is WHOLE_TENANT or branch_id in reach
    }


def staff_reachable_for_child(tenant, student, settings: MessagingSettings) -> dict:
    """``{user_id: label}`` of the staff a family may write to about *student*.

    The label is what the family reads under the person's name: "Class
    teacher, JSS1 A", the subjects taught, or "School office". A person who is
    several of these carries the first in that order.
    """
    from schools.vs_staff.models import TeachingAssignment
    from schools.vs_students.models import ClassEnrolment

    labels: dict = {}
    placement = (
        ClassEnrolment.objects.filter(
            tenant=tenant, student=student, is_active=True, session__status="ACTIVE",
        )
        .select_related("school_class__class_teacher")
        .first()
    )
    if placement is not None:
        klass = placement.school_class
        teacher = klass.class_teacher
        if teacher is not None:
            labels[teacher.user_id] = f"Class teacher, {klass.name}"
        if settings.subject_teachers:
            taught: dict = {}
            for row in TeachingAssignment.objects.filter(
                tenant=tenant, school_class=klass, session__status="ACTIVE",
            ).select_related("staff", "subject"):
                taught.setdefault(row.staff.user_id, set()).add(row.subject.name)
            for user_id, names in taught.items():
                labels.setdefault(user_id, ", ".join(sorted(names)))
    for user_id in office_users_covering(tenant, student.branch_id):
        labels.setdefault(user_id, "School office")
    if not labels:
        return {}
    eligible = set(
        current_staff_user_ids(tenant).filter(user_id__in=labels).values_list("user_id", flat=True)
    )
    for key in (PERM_FAMILY_SEND, PERM_VIEW):
        eligible &= set(key_holders(tenant, key).filter(pk__in=eligible).values_list("pk", flat=True))
    return {user_id: label for user_id, label in labels.items() if user_id in eligible}


def staff_profiles_by_user(tenant, user_ids):
    """``{user_id: StaffProfile}`` for *user_ids*, with the account loaded."""
    return {
        p.user_id: p
        for p in current_staff_profiles(tenant).filter(user_id__in=list(user_ids)).select_related("user")
    }


def family_children(tenant, caller, settings: MessagingSettings, *, search: str = "",
                    name_search: bool = True):
    """The pupils on the roll *caller* may start a family or pupil conversation about.

    The reverse of :func:`staff_reachable_for_child`: the caller is the child's
    class teacher this year, teaches the child's class this year (when the
    school lets families reach subject teachers), or is the school office for
    the child's branch.
    """
    from vs_rbac.evaluator import has_permission
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    from schools.vs_staff.models import TeachingAssignment
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import ClassEnrolment, Student, StudentGuardian

    placed = ClassEnrolment.objects.filter(
        tenant=tenant, student=OuterRef("pk"), is_active=True, session__status="ACTIVE",
        school_class__is_active=True,
    )
    reach = Q(Exists(placed.filter(school_class__class_teacher__user_id=caller.pk)))
    if settings.subject_teachers:
        taught = TeachingAssignment.objects.filter(
            tenant=tenant, staff__user_id=caller.pk, session__status="ACTIVE",
            school_class_id=OuterRef("school_class_id"),
        )
        reach |= Q(Exists(placed.filter(Exists(taught))))
    if has_permission(caller, PERM_FAMILY_OFFICE, tenant=tenant):
        covered = visible_branch_ids(caller, tenant)
        if covered is WHOLE_TENANT:
            reach |= Q(pk__isnull=False)
        else:
            reach |= Q(branch_id__in=tuple(covered or ()))
    students = (
        Student.objects.filter(tenant=tenant, status__in=PORTAL_STATUSES)
        .filter(reach)
        .annotate(child_class_name=Subquery(
            ClassEnrolment.objects.filter(
                student_id=OuterRef("pk"), is_active=True, session__status="ACTIVE",
            ).order_by("-pk").values("school_class__name")[:1]
        ))
        .select_related("branch", "user")
        .prefetch_related(Prefetch(
            "guardian_links",
            queryset=StudentGuardian.objects.select_related("guardian__user"),
        ))
    )
    term = (search or "").strip()
    if term and name_search:
        students = students.filter(
            Q(first_name__icontains=term) | Q(last_name__icontains=term)
            | Q(middle_name__icontains=term) | Q(student_number__icontains=term)
        )
    elif term:
        students = students.none()
    return students.order_by("last_name", "first_name", "pk")
