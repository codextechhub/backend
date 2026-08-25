"""Which rows a caller sees, and which branch a row they write belongs to.

Two functions, both used by every list and every create in this module, so that
the branch rule is written once rather than five times.

The read is deliberately **inclusive** and this is the whole difference between
this module and vs_procurement, which the FRD warns about by name. Procurement
narrows to ``branch_id__in=<the caller's>`` with no term for the shared rows,
because a purchase belongs to one place. A catalogue is the opposite case: the
shared rows are most of it, and filtering them out leaves a branch admin with an
empty screen whenever the school published at school level, which is the normal
case. vs_workflow already found and fixed that defect; its docstring still
records the symptom.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
from vs_tenants.references import resolve_branch_reference


def scope_to_visible_branches(queryset, user, tenant, field="branch"):
    """Narrow *queryset* to the shared rows plus the caller's own branches'.

    ``WHOLE_TENANT`` means no narrowing at all. An empty frozenset - every
    granted branch withdrawn - leaves the shared rows and nothing else, which is
    neither everything nor nothing and is the right answer for a catalogue:
    withdrawing a site withdraws that site, not the school's curriculum.
    """
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return queryset
    return queryset.filter(
        Q(**{f"{field}__isnull": True})
        | Q(**{f"{field}_id__in": tuple(sorted(visible))}),
    )


#: Sentinel for "the caller did not mention a branch at all".
#:
#: Distinct from ``None``, which is the caller explicitly choosing the whole
#: school - the design's "Applies to: The whole school" radio. The two have to
#: be told apart: omitting the field means "wherever I work", and a branch-bound
#: caller gets their own branch filled in; choosing the whole school is a claim
#: about the entire school, and a branch-bound caller may not make it.
UNSET = object()


def raised_branch(user, tenant, requested=UNSET, *, field="branch"):
    """The branch a row this caller writes belongs to.

    Mirrors ``vs_procurement.views.base._raised_branch``: the column semantics
    and the write rule are exactly what this module wants, even though the read
    narrowing is not.

    * A caller not narrowed to any branch may omit ``branch`` for a shared row,
      or name any branch of the tenant.
    * A caller narrowed to one branch has it filled in from theirs when omitted.
      Naming a different one is refused rather than retargeted, because quietly
      moving somebody's row to a branch they did not type is worse than saying no.
    * A caller narrowed to several must name one of theirs.
    * A caller whose granted branches have all been withdrawn may not create.

    A branch-bound caller may never create a shared row: a shared row is a
    statement about the whole school, and it is refused with 403 rather than
    422, because it is about who the caller is rather than what they typed.
    """
    explicit_school_wide = requested is None
    branch = (
        resolve_branch_reference(tenant, requested, field)
        if requested not in (UNSET, None, "")
        else None
    )
    visible = visible_branch_ids(user, tenant)

    if visible is WHOLE_TENANT:
        return branch

    if not visible:
        raise PermissionDenied(
            "Your access to every branch has been withdrawn, so you cannot "
            "create anything here. Ask a school administrator to restore it.",
        )

    if branch is None:
        if explicit_school_wide:
            # They asked for the whole school by name. A shared row is a
            # statement about every branch, including ones they do not work in,
            # so this is 403 rather than 422: it is about who they are, not
            # about what they typed.
            raise PermissionDenied(
                "You work in one branch, so you cannot create something that "
                "applies to the whole school. Ask a school administrator.",
            )
        if len(visible) == 1:
            return _only(tenant, visible)
        raise ValidationError({
            field: (
                "You work in more than one branch, so say which one this "
                "belongs to."
            ),
        })

    if branch.id not in visible:
        raise ValidationError({
            field: "You cannot create anything in that branch.",
        })
    return branch


def _only(tenant, visible):
    from vs_tenants.models import Branch

    return Branch.all_objects.filter(tenant=tenant, pk=next(iter(visible))).first()


def assert_within_parent(child_branch, parent_branch, *, parent_label):
    """A child may be no wider than its parent.

    If the parent is shared the child may be shared or in any branch. If the
    parent belongs to one branch the child must belong to that same branch: a
    shared Level under a branch-bound Program would claim the whole school while
    being reachable through one branch only.

    One function for Program/Department, Level/Program, SchoolClass/Level,
    Subject/Department and both ends of a SubjectOffering, rather than the same
    check written five times and drifting.
    """
    from ..exceptions import BranchScopeConflict

    if parent_branch is None:
        return
    if child_branch is not None and child_branch.id == parent_branch.id:
        return
    raise BranchScopeConflict(
        f"{parent_label} belongs to {parent_branch.name}, so this must belong "
        f"to {parent_branch.name} too.",
        parent=parent_label,
        parent_branch=parent_branch.name,
        given_branch=child_branch.name if child_branch else None,
    )


def branch_dimension_applies(tenant) -> bool:
    """Whether this school has more than one branch.

    Where a school has one branch the dimension recedes entirely: no branch
    field in the response, no branch filter on a list, no chip. Absent, not
    greyed out, because a control with a single option is noise. Nothing about
    the data changes with it - every row is written with a null branch, and the
    controls appear when a second branch opens without a row being rewritten.
    """
    from vs_tenants.models import Branch

    return Branch.all_objects.filter(tenant=tenant).count() > 1
