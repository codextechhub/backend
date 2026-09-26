"""How far a branch-bound caller may hand out access, and to whom.

A role grant decides which branches its holder sees, through
:func:`vs_rbac.scoping.visible_branch_ids`. A caller who could write a grant
wider than their own scope could therefore widen somebody else past it: the
Ikeja administrator gives an Ikeja teacher a school-wide role, and that teacher
now reads Lekki's pupils, staff and fees. Holding ``school.roles.assign`` says a
caller may grant roles; it does not say they may grant reach they do not have.

The rule, for a caller whose scope is a set of branches (a whole-tenant caller is
never narrowed here):

* the grant's reach must be non-empty and inside the caller's branches. The
  reach is the grant's own branch where it names one, and the role's selected
  branches where it does not. A grant naming neither reaches the whole school,
  and only a whole-tenant caller may write one;
* the holder must be somebody the caller manages: posted only to the caller's
  branches. A school-wide person, or one also posted elsewhere, is visible to a
  branch administrator and not theirs to change.

The same check applies to revoking or replacing a grant, since removing the
registrar's school-wide role from Ikeja is as much a change to a shared person
as adding one.

Refusals are ``ValidationError`` keyed by the field that caused them, so a form
can show the message under the control the reader has to change.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError

from .scoping import WHOLE_TENANT, visible_branch_ids

REACH_OUTSIDE = (
    "You can only grant roles that reach your own branches. A school-wide "
    "administrator can grant this one."
)
HOLDER_SHARED = (
    "This person works across more than your branch, so only a school-wide "
    "administrator can change their roles."
)


def grant_reach_ids(role, branch) -> set:
    """The branch ids a grant reaches; empty means the whole school."""
    if branch is not None:
        return {getattr(branch, "pk", branch)}
    return set(role.branch_ids) if role is not None else set()


def holder_posting_ids(user) -> set:
    """The branches an account is posted to; empty means school-wide."""
    ids = set(user.additional_branches.values_list("pk", flat=True))
    if user.branch_id is not None:
        ids.add(user.branch_id)
    return ids


def assert_caller_may_grant(caller, tenant, role, branch, *, holder=None) -> None:
    """Refuse a grant (or a change to one) that reaches past the caller's branches.

    Applies only to a caller inside *tenant*. Branch grants exist only in the
    caller's own tenant, and a platform operator acting on a school is
    governed by the platform keys and entity scoping, not by a branch.
    """
    if getattr(caller, "tenant_id", None) != getattr(tenant, "pk", None):
        return
    visible = visible_branch_ids(caller, tenant)
    if visible is WHOLE_TENANT:
        return
    reach = grant_reach_ids(role, branch)
    if not reach or not reach <= visible:
        raise ValidationError({"branch": REACH_OUTSIDE})
    if holder is not None and getattr(holder, "pk", None) != getattr(caller, "pk", None):
        postings = holder_posting_ids(holder)
        if not postings or not postings <= visible:
            raise ValidationError({"user": HOLDER_SHARED})
