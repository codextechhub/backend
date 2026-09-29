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


#: The only roles a school may hand out before it goes live.
#:
#: Onboarding has one administrator in it, so there is nobody to review what
#: they grant. A bursar invited as Payout Approver during onboarding holds that
#: grant the moment the school goes live, and no second pair of eyes ever saw
#: it. Matched on the role KEY rather than the name, because the name is the
#: school's to rename and the key is not. The Add form and the staff import
#: both hold to it.
ONBOARDING_ROLE_KEYS = ("school_admin", "branch_admin")

#: The refusal for any other role while a school is onboarding.
ONBOARDING_ROLE_REFUSAL = (
    "Until this school goes live, only School Admin and Branch Admin can be "
    "given out."
)


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

    Returns ``(granted, already_held, pending)``, each a list of
    ``StaffProfile``. Somebody who already holds that role at that reach, or
    already has it waiting for approval, is reported and left alone, because
    granting it twice is what the database's own constraints exist to refuse
    and reporting it is what the screen asks for.

    Each grant goes through :func:`vs_rbac.services.grant_role`, so a role
    carrying restricted keys the actor does not hold raises one approval
    request per person rather than being written. A bulk grant is not a way
    round the rule a single grant keeps.
    """
    from vs_rbac.models import TenantUserRoleAssignment
    from vs_rbac.services import grant_role, pending_grant_requests

    if branch is not None:
        role_ids = role.branch_ids
        if len(role_ids) > 1:
            raise ValidationError({
                "branch": "This role grants all its selected branches. Leave the assignment branch empty.",
            })
        if role_ids and branch.pk not in role_ids:
            raise ValidationError({"branch": "This branch is outside the role's reach."})

    waiting = pending_grant_requests(tenant=tenant).filter(role=role)
    waiting = waiting.filter(branch=branch) if branch is not None else waiting.filter(branch__isnull=True)
    waiting_user_ids = set(waiting.values_list("user_id", flat=True))

    granted, already, pending = [], [], []
    for person in people:
        if person.user_id in waiting_user_ids:
            pending.append(person)
            continue
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
        outcome = grant_role(
            tenant=tenant, user=person.user, role=role, branch=branch, actor=actor,
        )
        (pending if outcome.pending else granted).append(person)
    return granted, already, pending


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
            ONBOARDING_ROLE_REFUSAL if onboarding_keys is not None
            else "That is not a role this school can give out."
        )
        raise ValidationError({"role": message})
    return role


def find_starting_role(tenant):
    """This school's active starting role, or ``None`` where it has none.

    The starting role is the school's choice (``staff.starting_role``, default
    ``teacher``). Adding somebody and deciding what they may reach are two jobs
    held by two people: the key that adds staff is not the key that assigns
    roles. So the add path grants this one baseline role and nothing else, and
    anything wider or narrower is a role admin's change afterwards.
    """
    from vs_rbac.models import TenantRoleTemplate

    from .rules import starting_role_key

    return TenantRoleTemplate.objects.filter(
        tenant=tenant, status="ACTIVE", key=starting_role_key(tenant),
    ).first()


def starting_role(tenant, *, requested="", actor=None):
    """The role a new member of staff at a live school is granted.

    ``requested`` is whatever the caller sent as ``role``. Naming the starting
    role itself is accepted; naming any other is refused rather than quietly
    replaced, so a client still offering a role picker learns that the choice
    is not its to make instead of believing it was honoured.

    A school whose starting role is no longer active cannot add anybody until
    it is restored or another is chosen, and is told so by the role's own name,
    rather than creating accounts that sign in and reach nothing.

    The starting role is never a way round the grant ceiling. Where it carries
    restricted permissions *actor* does not hold, the add is refused with a 403
    naming the role, because the only other outcome is an account holding a
    restricted grant nobody entitled to give it approved.
    """
    from rest_framework.exceptions import PermissionDenied
    from vs_rbac.models import TenantRoleTemplate

    from .rules import starting_role_key

    key = starting_role_key(tenant)
    role = find_starting_role(tenant)
    if role is None:
        named = TenantRoleTemplate.objects.filter(tenant=tenant, key=key).first()
        name = named.name if named is not None else key
        raise ValidationError({
            "role": (
                f"This school has no active {name} role, which every new member "
                f"of staff starts with. Restore it in Roles & Permissions, or "
                f"choose another starting role in Settings, Staff."
            ),
        })
    if requested not in ("", None, role.key, str(role.pk)):
        raise ValidationError({
            "role": (
                f"New staff start as {role.name}. Other roles are given from "
                f"Roles & Permissions once they are added."
            ),
        })
    if actor is not None:
        from vs_rbac.services import grant_needs_approval

        if grant_needs_approval(actor, role):
            raise PermissionDenied(
                f"New staff start as {role.name}, which carries restricted "
                f"permissions you do not hold, so you cannot add staff. Ask an "
                f"administrator who holds them, or choose another starting role "
                f"in Settings, Staff.",
            )
    return role
