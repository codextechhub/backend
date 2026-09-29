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

Two more changes reach past a branch the same way, and are judged here too.

**What a role means.** A role's permissions, field switches, name, status and
branch set bind every holder of it, and its holders can only ever sit inside
its own branches (an empty set meaning the whole school). So changing a role is
changing a row whose branch set is the role's, and
:func:`vs_rbac.scoping.caller_may_change` already answers who may do that: a
school-wide role needs a whole-school caller, an Ikeja role an Ikeja one.
:func:`assert_caller_may_define` asks it before a role is edited, retired,
deleted, has its field access changed or has a change request raised on it,
and :func:`assert_caller_may_reach` asks it of the branch set a role is being
given, so the Ikeja administrator cannot widen her own Ikeja role to the whole
school and thereby hand every Ikeja holder of it Lekki's records.

**One person's exceptions.** A permission or field exception on a person
travels with them wherever their access reaches.
:func:`assert_caller_may_change_person` judges the person by their postings
and their reach together: a branch-bound caller may set one only on somebody
posted inside their branches whose roles reach nowhere else. The same answer
serves anything else that follows a person across branches, such as an
administrator changing somebody's approval delegation. The school-wide
bursar posted at Ikeja is Ikeja's to read and not Ikeja's to change, because a
DENY written there removes her access at Lekki too.

Those three refuse with 403 ``SHARED_RECORD_READ_ONLY``
(:class:`vs_rbac.exceptions.SharedRecordReadOnly`) before anything is written.
Like the grant check, each applies only to a caller inside the tenant.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError

from .scoping import WHOLE_TENANT, assert_caller_may_change, visible_branch_ids

REACH_OUTSIDE = (
    "You can only grant roles that reach your own branches. A school-wide "
    "administrator can grant this one."
)
HOLDER_SHARED = (
    "This person works across more than your branch, so only a school-wide "
    "administrator can change their roles."
)
ROLE_SHARED = (
    "Only a school-wide administrator can change what a role can {verb}. Ask "
    "one to change it, or create a role for your branch."
)
ROLE_OTHER_BRANCHES = (
    "This role reaches branches you do not work in, so only an administrator "
    "who covers them can change what it can {verb}."
)
REACH_SHARED = (
    "Only a school-wide administrator can make a role reach every branch. "
    "Choose your own branch for it."
)
REACH_OTHER_BRANCHES = "You can only give a role branches you work in."
EXCEPTION_SHARED = (
    "This person's access reaches beyond your branch, so only a school-wide "
    "administrator can change their exceptions."
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


def _inside_tenant(caller, tenant) -> bool:
    """Whether *caller* belongs to *tenant*, the only case a branch narrows."""
    return getattr(caller, "tenant_id", None) == getattr(tenant, "pk", None)


def assert_caller_may_define(caller, tenant, role, *, verb: str = "do") -> None:
    """Refuse a change to what *role* means unless the caller covers its branches (403).

    ``verb`` completes the refusal: "do" for permissions, status, name and
    deletion, "see" for field access.
    """
    if not _inside_tenant(caller, tenant):
        return
    ids = role.branch_ids
    template = ROLE_OTHER_BRANCHES if ids else ROLE_SHARED
    assert_caller_may_change(caller, tenant, ids, message=template.format(verb=verb))


def assert_caller_may_reach(caller, tenant, branch_ids) -> None:
    """Refuse giving a role a branch set the caller does not cover (403).

    An empty *branch_ids* is a school-wide role, which only a whole-school
    caller may create or widen a role into.
    """
    if not _inside_tenant(caller, tenant):
        return
    ids = list(branch_ids)
    assert_caller_may_change(
        caller, tenant, ids, message=REACH_OTHER_BRANCHES if ids else REACH_SHARED,
    )


def assert_caller_may_change_person(caller, tenant, holder, *,
                                    message: str = EXCEPTION_SHARED) -> None:
    """Refuse a change that follows *holder* past the caller's branches (403).

    The person's branch set is their postings together with the branches
    their roles reach; a school-wide posting or a whole-school reach makes it
    the whole school.
    """
    if not _inside_tenant(caller, tenant):
        return
    postings = holder_posting_ids(holder)
    reach = visible_branch_ids(holder, tenant)
    ids = set() if not postings or reach is WHOLE_TENANT else postings | set(reach)
    assert_caller_may_change(caller, tenant, ids, message=message)
