"""Which accounts may close themselves, and how, for pupils and parents.

The account engine lets a product decide, per account, what happens when its
holder taps Delete my account (:mod:`vs_user.services.account_closure`). This
app answers for the two kinds of account it owns:

* a **pupil's** sign-in (``Student.user``) never closes by itself. The request
  goes to the pupil's branch, the sign-in keeps working, and an administrator
  closes it or declines;
* a **parent's** sign-in (``Guardian.user``) closes at once, because the person
  holding it is an adult deciding for themselves. Their guardian record on the
  child, with the name, phone and email the school reaches them on, is not
  touched. The request carries the branch of their children when all of them
  attend one branch, and none when they span several.

An account that is also somebody's working login, such as a teacher whose own
child attends, is answered by nobody here: closing it would end their job's
sign-in, so it keeps the refusal that points at an administrator.
"""
from __future__ import annotations

KIND_PUPIL = "pupil"
KIND_PARENT = "parent"


def _holds_working_access(user) -> bool:
    """Whether *user* is somebody's working login as well as a pupil's or parent's."""
    if user.is_staff or user.is_superuser or user.is_platform_user:
        return True
    if user.tenant_role_assignments.exists():
        return True
    return hasattr(user, "staff_profile")


def _children_branch_id(user):
    """The one branch all of a parent's children on the roll attend, else None.

    A parent whose children are all off the roll is placed by every child they
    have ever had linked, so a parent of leavers is still reviewed at the
    branch the family belonged to.
    """
    from .constants import PORTAL_STATUSES
    from .models import StudentGuardian

    links = StudentGuardian.all_objects.filter(
        tenant_id=user.tenant_id, guardian__user=user,
    )
    branches = set(
        links.filter(student__status__in=PORTAL_STATUSES)
        .values_list("student__branch_id", flat=True)
    ) or set(links.values_list("student__branch_id", flat=True))
    return next(iter(branches)) if len(branches) == 1 else None


def closure_policy(user):
    """The closure policy for a pupil's or a parent's account, else None."""
    from vs_user.services.account_closure import MODE_CLOSE, MODE_REQUEST, ClosurePolicy

    from .models import Guardian, Student
    from .services.guardian_portal_access import is_parent_only

    pupil_branch = (
        Student.all_objects.filter(user=user, tenant_id=user.tenant_id)
        .values_list("branch_id", flat=True).first()
    )
    if pupil_branch is not None:
        if _holds_working_access(user):
            return None
        return ClosurePolicy(mode=MODE_REQUEST, branch_id=pupil_branch, kind=KIND_PUPIL)

    if Guardian.all_objects.filter(user=user, tenant_id=user.tenant_id).exists():
        if not is_parent_only(user):
            return None
        return ClosurePolicy(
            mode=MODE_CLOSE, branch_id=_children_branch_id(user), kind=KIND_PARENT,
        )
    return None


def register() -> None:
    """Tell the account engine how pupils and parents may close their accounts."""
    from vs_user.services.account_closure import register_account_closure_policy

    register_account_closure_policy("pupils_and_parents", closure_policy)
