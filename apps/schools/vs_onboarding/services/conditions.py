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


#: The profile fields a school must have filled in before it trades. Slug and
#: code are allocated by creation and are here to catch a repaired or imported
#: row that lost one, not because a school is expected to type them.
SCHOOL_METADATA_FIELDS = (
    "name", "slug", "code", "ownership_type", "term_structure", "currency",
)


def _has_school_metadata(tenant, school) -> bool:
    if school is None:
        return False
    return all(
        str(getattr(school, field, "") or "").strip()
        for field in SCHOOL_METADATA_FIELDS
    )


def _has_set_of_books(tenant, school) -> bool:
    """A ledger entity for this tenant.

    Books are provisioned for every school at creation, best effort, so this
    task is normally already satisfied when the school first opens the control
    room. It stays required because best effort means some school's books did
    not arrive, and this is the step that says so.
    """
    from vs_finance.models import LedgerEntity

    return LedgerEntity.objects.filter(tenant=tenant).exists()


def _has_initial_data(tenant, school) -> bool:
    """At least one import that fully succeeded. IMPORT_SUCCEEDED only.

    **A partial import does not complete this step, by decision (2026-08-17),
    and this condition must not be relaxed to accept one.** Where the two
    disagreed, the import screen was the side that was corrected: it used to
    report a partial import as success, and it no longer does. Onboarding is
    the strict side on purpose, because this step is the school's own statement
    that its data is in, and half a roll of students is not.

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
    TaskKey.FIRST_ADMIN: _has_first_admin,
    TaskKey.ROLE_BASELINE: _has_role_baseline,
    TaskKey.SCHOOL_METADATA: _has_school_metadata,
    TaskKey.SET_OF_BOOKS: _has_set_of_books,
    TaskKey.INITIAL_DATA: _has_initial_data,
    TaskKey.STAFF_INVITATIONS: _has_staff_invitations,
}

#: What to tell the school when a condition refuses. Phrased as the thing they
#: still have to do, not as the predicate that returned False.
CONDITION_REASONS = {
    TaskKey.FIRST_ADMIN: (
        "This school has no active administrator holding the school "
        "administrator role. Re-send the invitation and try again."
    ),
    TaskKey.ROLE_BASELINE: (
        "The school administrator role carries no permissions yet."
    ),
    TaskKey.SCHOOL_METADATA: (
        "Complete the school profile first: name, code, ownership type, term "
        "structure and currency are all required."
    ),
    TaskKey.SET_OF_BOOKS: (
        "This school has no set of books yet. Contact support to have them "
        "provisioned."
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


def condition_reason(key: str) -> str:
    return CONDITION_REASONS.get(key, "This onboarding step is not complete yet.")
