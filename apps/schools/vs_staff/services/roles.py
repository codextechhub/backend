"""Granting one role to a selection of people.

This module owns no role model and no grant model. What a person may do is
``vs_rbac.TenantUserRoleAssignment``, which is richer than anything worth
rebuilding here: a tenant, an optional branch, an assignment status, who granted
it, who revoked it, when and why, and two split unique constraints that let one
person hold one role at two branches. A second table would be a second answer to
what somebody may do, and the evaluator reads only the first.

So a bulk grant is a loop over the same rows a single grant writes, inside one
transaction, obeying exactly the rules a single grant obeys. What it adds is the
two things a loop cannot get for itself: **the whole selection is resolved and
narrowed before anything is written**, so a partial bulk never happens, and
**anybody who already holds the role is reported rather than granted twice**.

FRD M12 v2.1, FR-005 and FR-019.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..constants import PERM_ROLES_ASSIGN  # noqa: F401  (the view's key, named here)

#: The role every member of staff added at a live school starts with.
#:
#: Adding somebody and deciding what they may reach are two different jobs held
#: by two different people: the key that adds staff is not the key that assigns
#: roles. So the add path grants this one baseline role and nothing else, and
#: anything wider or narrower is a role admin's change afterwards. Matched on
#: the KEY, because the name is the school's to rename and the key is not.
STARTING_ROLE_KEY = "teacher"


def active_grants(staff):
    """This person's live grants, newest first, with their roles loaded."""
    return [
        grant for grant in staff.user.tenant_role_assignments.all()
        if grant.assignment_status == "ACTIVE"
    ]


def revoked_grants(staff):
    """History, never deleted.

    A revoked grant is how a school answers "what did this person used to be
    able to do", which is the question asked after something has gone wrong.
    """
    return [
        grant for grant in staff.user.tenant_role_assignments.all()
        if grant.assignment_status == "REVOKED"
    ]


def overrides_for(staff, tenant):
    """Per-person permission exceptions, or None where the caller may not see them.

    ``school.user_overrides.view`` is CRITICAL and school-admin only, and it is
    deliberately restricted so that a person cannot learn that exceptions exist
    on their own account. The caller decides whether to ask; this returns the
    rows and never a partial view of them.
    """
    from vs_rbac.models import UserPermissionOverride

    return list(
        UserPermissionOverride.objects.filter(tenant=tenant, user=staff.user)
        .select_related("permission")
        .order_by("permission__key")
    )


@transaction.atomic
def grant_to_many(*, tenant, role, branch, people, actor):
    """Give one role at one reach to everybody in ``people``.

    One role and one reach for the whole selection: a bulk action that varied
    per person would be a form pretending to be a spreadsheet.

    Returns ``(granted, already_held)``, both as lists of ``StaffProfile``.
    Somebody who already holds that role at that reach is reported and left
    alone, because granting it twice is what the database's own constraints
    exist to refuse and reporting it is what the screen asks for.
    """
    from vs_rbac.models import TenantUserRoleAssignment

    if branch is not None:
        role_ids = role.branch_ids
        if len(role_ids) > 1:
            raise ValidationError({
                "branch": "This role grants all its selected branches. Leave the assignment branch empty.",
            })
        if role_ids and branch.pk not in role_ids:
            raise ValidationError({"branch": "This branch is outside the role's reach."})

    granted, already = [], []
    for person in people:
        exists = TenantUserRoleAssignment.objects.filter(
            tenant=tenant, user=person.user, role=role, branch=branch,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).exists()
        if exists:
            already.append(person)
            continue
        # A whole-tenant grant dominates in visible_branch_ids, so pinning the
        # same role to a branch on top of one confers nothing. Skip rather than
        # write a row that changes nothing and reads as though it did.
        if branch is not None and TenantUserRoleAssignment.objects.filter(
            tenant=tenant, user=person.user, role=role, branch__isnull=True,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        ).exists():
            already.append(person)
            continue
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=person.user, role=role, branch=branch,
            assigned_by=actor,
        )
        granted.append(person)
    return granted, already


def resolve_role(tenant, key_or_id, *, onboarding_keys=None):
    """One of this school's own active roles, resolved inside this school.

    ``onboarding_keys`` narrows the catalogue while the school is still PENDING,
    and the narrowing is enforced here rather than only offered by a dropdown: a
    crafted request must not be able to grant Payout Approver during onboarding,
    when there is nobody to review what was granted.
    """
    from vs_rbac.models import TenantRoleTemplate

    queryset = TenantRoleTemplate.objects.filter(tenant=tenant, status="ACTIVE")
    if onboarding_keys is not None:
        queryset = queryset.filter(key__in=onboarding_keys)

    role = None
    if isinstance(key_or_id, int) or str(key_or_id).isdigit():
        role = queryset.filter(pk=int(key_or_id)).first()
    if role is None:
        role = queryset.filter(key=key_or_id).first()
    if role is None:
        message = (
            "Until this school goes live, only School Admin and Branch Admin "
            "can be given out."
            if onboarding_keys is not None
            else "That is not a role this school can give out."
        )
        raise ValidationError({"role": message})
    return role


def find_starting_role(tenant):
    """This school's active starting role, or ``None`` where it has none."""
    from vs_rbac.models import TenantRoleTemplate

    return TenantRoleTemplate.objects.filter(
        tenant=tenant, status="ACTIVE", key=STARTING_ROLE_KEY,
    ).first()


def starting_role(tenant, *, requested=""):
    """The role a new member of staff at a live school is granted.

    ``requested`` is whatever the caller sent as ``role``. Naming the starting
    role itself is accepted; naming any other is refused rather than quietly
    replaced, so a client still offering a role picker learns that the choice
    is not its to make instead of believing it was honoured.

    A school that has retired its Teacher role cannot add anybody until it is
    restored, and is told so, rather than creating accounts that sign in and
    reach nothing.
    """
    role = find_starting_role(tenant)
    if role is None:
        raise ValidationError({
            "role": (
                "This school has no active Teacher role, which every new member "
                "of staff starts with. Restore it in Roles & Permissions first."
            ),
        })
    if requested not in ("", None, role.key, str(role.pk)):
        raise ValidationError({
            "role": (
                "New staff start as Teacher. Other roles are given from Roles "
                "& Permissions once they are added."
            ),
        })
    return role
