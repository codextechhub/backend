"""Where somebody is based, and the roster that reads it three ways.

A posting and a reach are different facts and this module keeps them apart.
The posting is one branch or the whole school, and it is this table's column.
The reach is which branches a person can actually work in, it comes from their
role grants, and it is computed by ``vs_rbac`` rather than stored here, so it
cannot go stale.

**Moving a posting never moves a grant**, and the response says so as well as
the confirmation. A bulk action that quietly re-pinned grants would change what
people may do while claiming to change where they sit.

FRD M12 v2.1, FR-010 and section 6.
"""
from __future__ import annotations

from django.db import transaction

from vs_rbac.scoping import (
    WHOLE_TENANT,
    visible_branch_ids,
    visible_branch_ids_for,
)
from vs_tenants.models import Branch
from vs_tenants.references import resolve_branch_reference

from ..exceptions import BranchNotInService
from . import audit

#: The caller did not mention a branch at all, which is different from naming
#: none. "Across the whole school" is a choice a form offers explicitly, and a
#: missing key must not be read as somebody making it.
UNSET = object()


def resolve_posting(tenant, requested):
    """A branch, or None for school-wide. Anything unusable collapses to 404.

    ``resolve_branch_reference`` answers unknown, malformed and another
    tenant's identically, so a posting field cannot be used to enumerate the
    sites of schools the caller has never heard of.
    """
    if requested in (UNSET, None, ""):
        return None
    branch = resolve_branch_reference(tenant, requested, "branch")
    if branch.status not in Branch.IN_SERVICE_STATES:
        raise BranchNotInService(
            f"{branch.name} is not in service, so nobody can be posted to it.",
            branch=branch.name,
        )
    return branch


@transaction.atomic
def set_posting(staff, branch, *, actor, reason=""):
    """Write the posting on the record and on the account, together.

    ``User.branch`` keeps its job as the caller's home posting, and the identity
    layer's fallback narrowing reads it. Letting the two drift would mean a
    person whose staff record says Ikeja and whose session still narrows to
    Lekki, which is the kind of disagreement nobody notices until it decides
    what somebody can see.

    Returns the classes this person teaches at the branch they are leaving, so
    the caller can warn and name them. Warn, never refuse: a school moving a
    teacher mid-term is doing it deliberately.
    """
    previous = staff.branch_id
    if previous == getattr(branch, "pk", None):
        return []

    stranded = _assignments_left_behind(staff)

    staff.branch = branch
    staff.save(update_fields=["branch", "updated_at"])

    user = staff.user
    user.branch = branch
    user.save(update_fields=["branch", "updated_at"])

    audit.emit_posting_changed(staff, previous, actor=actor)
    return stranded


def _assignments_left_behind(staff):
    """Classes at the person's current posting that they teach.

    Named rather than counted, and read before the move so the branch being left
    is still the one on the record.
    """
    if staff.branch_id is None:
        return []
    from schools.vs_academics.models import AcademicSession, SessionStatus

    active = AcademicSession.all_objects.filter(
        tenant_id=staff.tenant_id, status=SessionStatus.ACTIVE,
    ).values_list("pk", flat=True).first()
    if active is None:
        return []
    rows = (
        staff.teaching_assignments.filter(
            session_id=active, school_class__branch_id=staff.branch_id,
        )
        .select_related("subject", "school_class")
        .order_by("school_class__name", "subject__name")
    )
    return [f"{row.school_class.name} {row.subject.name}" for row in rows]


def roster(tenant, user, branch):
    """Everybody who works at one branch, in the three groups that say why.

    One flat list would tell an Ikeja administrator that Mrs. Nwankwo is theirs.
    She is not at Ikeja, she is at the school, and she appears in every branch's
    roster for a reason that is not the same as being posted there.

    **People who have left stay on it, and say so.** Dropping them would hide
    that Mrs. Bello was ever at Lekki from the only screen that answers who is,
    and a school looking for last year's arrangements would find nothing. Their
    row carries ``on_roll: false`` instead, so a screen can draw them as
    finished and refuse to move a posting that no longer means anything.

    Returns ``(posted_here, reaching_here, school_wide, via)``. ``via`` maps a
    reaching person's staff id to the roles that carry them here, because
    "reaching through a role" without naming the role leaves a reader with
    nothing to go and change.

    The first and third groups are movable and the caller says so. The middle
    one is not: somebody reaching Ikeja through a branch-pinned grant is moved
    by changing that grant, not by moving where they are based.
    """
    from ..models import StaffProfile
    from ..serializers import STAFF_LIST_PREFETCH

    people = list(
        StaffProfile.objects.filter(tenant=tenant)
        .select_related("user", "branch")
        .prefetch_related(*STAFF_LIST_PREFETCH)
    )

    # One query for everybody's reach rather than one per person. The single
    # reader memoises on the user INSTANCE, which is right for request.user and
    # useless here: each row carries its own, so the cache never hit and a
    # hundred-and-nine-strong school ran a hundred and nine queries to draw one
    # roster.
    reach = visible_branch_ids_for([person.user for person in people], tenant)

    posted_here, reaching_here, school_wide = [], [], []
    via = {}
    for person in people:
        if person.branch_id == branch.pk:
            posted_here.append(person)
        elif person.branch_id is None:
            school_wide.append(person)
        elif _reaches(reach.get(person.user_id), branch.pk):
            reaching_here.append(person)
            via[person.pk] = _via_roles(person, branch.pk)
    return posted_here, reaching_here, school_wide, via


def _via_roles(staff, branch_id) -> list:
    """The roles that carry one person to a branch they are not posted to.

    Read off the grants the roster already prefetched, so naming them costs no
    query.

    Each says whether it is pinned to THIS branch or holds no pin at all, and
    the two are not the same problem. A pinned grant is unpinned and the person
    stops reaching here. A school-wide grant reaches every branch by being
    pinned to none, so narrowing it takes them off every other branch's roster
    at the same time - and a screen that told an administrator to "change which
    branch that role reaches" without saying which kind they were looking at
    would have them do the second while intending the first.
    """
    seen = {}
    for grant in staff.user.tenant_role_assignments.all():
        if grant.assignment_status != "ACTIVE":
            continue
        if grant.branch_id not in (branch_id, None):
            continue
        role = getattr(grant, "role", None)
        if role is None:
            continue
        name = role.name or role.key
        # A role held both ways reaches here on its own pin, which is the one
        # somebody would change, so the narrower reading wins.
        if name not in seen or grant.branch_id is not None:
            seen[name] = {"name": name, "school_wide": grant.branch_id is None}
    return [seen[name] for name in sorted(seen)]


def _reaches(visible, branch_id) -> bool:
    """Whether one person's already-resolved scope extends to a branch."""
    if visible is WHOLE_TENANT:
        return True
    return branch_id in (visible or ())


def reach_of(staff):
    """The branches a person's roles reach, with the role behind each.

    Returns ``(is_school_wide, [(branch, [role_name, ...]), ...])``. A
    whole-tenant grant reads as school-wide and means every branch; an empty
    list where the person holds only revoked grants is a real answer meaning
    they reach the school-wide rows and nothing else, and must not be rendered
    as though no narrowing applied.
    """
    grants = [
        grant for grant in staff.user.tenant_role_assignments.all()
        if grant.assignment_status == "ACTIVE"
    ]
    if any(grant.branch_id is None for grant in grants):
        return True, []

    by_branch: dict[int, list[str]] = {}
    for grant in grants:
        role = getattr(grant, "role", None)
        name = (role.name or role.key) if role is not None else ""
        by_branch.setdefault(grant.branch_id, []).append(name)

    branches = {
        branch.pk: branch
        for branch in Branch.all_objects.filter(pk__in=by_branch)
    }
    rows = [
        (branches[branch_id], sorted(set(names)))
        for branch_id, names in by_branch.items()
        if branch_id in branches
    ]
    rows.sort(key=lambda row: row[0].name)
    return False, rows
