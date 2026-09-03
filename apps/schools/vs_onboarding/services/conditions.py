"""What the platform can check for itself before it believes a step is done.

A task with a condition here cannot be marked DONE while the condition is
false: the school says "we have set up our branches" and the platform answers
with what it can actually see. A task with no condition is completed on the
school's word, which is the honest position for a step whose backend does not
exist yet, and is stated here rather than faked with a stub that always returns
True.

Every query names its tenant explicitly. Several of these models carry a
tenant-aware default manager whose ambient scope is the *caller's* tenant, and
a platform reviewer approving a school's go-live is precisely the case where
those two differ.
"""
from __future__ import annotations

from ..constants import TaskKey


def _has_first_admin(tenant, school) -> bool:
    """A working administrator, not merely an invited one.

    School creation swallows its own failures when provisioning the first admin
    (it returns None and leaves the invitation QUEUED rather than losing the
    school), so "an administrator exists and can sign in" is a fact to verify
    and never a fact to assume. Branch is NULL deliberately: a person pinned to
    one site is not the school's administrator.
    """
    from vs_rbac.models import TenantUserRoleAssignment

    return TenantUserRoleAssignment.objects.filter(
        tenant=tenant,
        role__tenant=tenant,
        role__key="school_admin",
        branch__isnull=True,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        user__tenant=tenant,
        user__is_active=True,
        user__status="ACTIVE",
    ).exists()


def _has_role_baseline(tenant, school) -> bool:
    """The school_admin template exists for this tenant and grants something.

    An empty role is the failure this catches: provisioning from the prebuilt
    template can create the template and then fail to copy its permissions, and
    a role that grants nothing looks identical to a role that works until
    somebody tries to use it.
    """
    from vs_rbac.models import TenantRolePermission

    return TenantRolePermission.objects.filter(
        role__tenant=tenant,
        role__key="school_admin",
        granted=True,
    ).exists()


def _has_school_metadata(tenant, school) -> bool:
    """Every required profile field filled in.

    The list of fields is the School model's own
    (``vs_schools.models.REQUIRED_PROFILE_FIELDS``, read here through
    ``missing_profile_fields``) and deliberately not a second copy kept in this
    module. The profile screen tells the admin which fields are still empty
    from the same source, so the screen and this gate cannot name different
    fields - which they did while each app owned its own tuple.
    """
    if school is None:
        return False
    return not school.missing_profile_fields()


def _has_default_roles(tenant, school) -> bool:
    """Both halves of "can somebody actually operate this school?".

    One card, two facts, and neither is redundant: school creation can produce
    an administrator whose role grants nothing, and it can produce a working
    role with nobody holding it. They were separate checklist rows until
    2026-08-22; the design presents them as one card, so they are one row that
    is refused unless both hold.
    """
    return _has_first_admin(tenant, school) and _has_role_baseline(tenant, school)


def _has_initial_data(tenant, school) -> bool:
    """At least one import that fully succeeded. IMPORT_SUCCEEDED only.

    **A partial import does not complete this step, by decision (2026-08-17),
    and this condition must not be relaxed to accept one.** Onboarding is the
    strict side on purpose, because this step is the school's own statement that
    its data is in, and half a roll of students is not. Where the import screen
    disagrees, the import screen is the side to correct.

    Onboarding reads the import engine's result and never validates or imports
    anything itself.
    """
    from vs_import_data.models import ImportBatch, ImportBatchStatusChoices

    return ImportBatch.all_objects.filter(
        tenant=tenant,
        status=ImportBatchStatusChoices.IMPORT_SUCCEEDED,
    ).exists()


def _has_staff_invitations(tenant, school) -> bool:
    """Somebody beyond the first administrator has an account here."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(tenant=tenant).count() > 1


#: Task key to condition. A key absent from this map has no machine-checkable
#: condition and is completed on the school's word: ACADEMIC_STRUCTURE has no
#: backend to check against at all.
TASK_CONDITIONS = {
    TaskKey.DEFAULT_ROLES: _has_default_roles,
    TaskKey.SCHOOL_METADATA: _has_school_metadata,
    TaskKey.INITIAL_DATA: _has_initial_data,
    TaskKey.STAFF_INVITATIONS: _has_staff_invitations,
}

#: What to tell the school when a condition refuses. Phrased as the thing they
#: still have to do, not as the predicate that returned False.
CONDITION_REASONS = {
    # One card, so one sentence - but it must name which half failed, or a
    # school reads "roles are not right" and has no idea whether to chase the
    # invitation or the permissions. Resolved per call by ``condition_reason``.
    TaskKey.DEFAULT_ROLES: (
        "Your roles are not ready yet. Check that an administrator has "
        "accepted their invitation and that the school administrator role "
        "carries its permissions."
    ),
    TaskKey.SCHOOL_METADATA: (
        "Complete the school profile first: name, code, ownership type, term "
        "structure and currency are all required."
    ),
    # Says "fully" out loud, because the school may be looking at an import
    # that finished with some rows rejected and wondering why this step will
    # not close. It will not, deliberately.
    TaskKey.INITIAL_DATA: (
        "No data import has completed in full for this school yet. An import "
        "that finished with rejected rows does not complete this step."
    ),
    TaskKey.STAFF_INVITATIONS: "No staff have been invited beyond the first administrator.",
}


def condition_holds(key: str, tenant, school) -> bool:
    """True when ``key`` has no condition, or its condition is satisfied."""
    check = TASK_CONDITIONS.get(key)
    if check is None:
        return True
    return bool(check(tenant, school))


#: The half-specific sentences behind DEFAULT_ROLES. Kept because a merged card
#: with a merged sentence tells a school its roles are wrong and nothing about
#: which thing to go and fix.
DEFAULT_ROLES_REASONS = {
    "no_admin": (
        "This school has no active administrator holding the school "
        "administrator role. Re-send the invitation and try again."
    ),
    "no_permissions": "The school administrator role carries no permissions yet.",
}


def condition_reason(key: str, tenant=None, school=None) -> str:
    """Why ``key`` was refused, as specifically as the platform can say.

    ``tenant``/``school`` are optional so existing callers keep working, but
    passing them is what lets the merged roles card name the half that failed
    rather than describing both.
    """
    if key == TaskKey.DEFAULT_ROLES and tenant is not None:
        if not _has_first_admin(tenant, school):
            return DEFAULT_ROLES_REASONS["no_admin"]
        if not _has_role_baseline(tenant, school):
            return DEFAULT_ROLES_REASONS["no_permissions"]
    return CONDITION_REASONS.get(key, "This onboarding step is not complete yet.")
