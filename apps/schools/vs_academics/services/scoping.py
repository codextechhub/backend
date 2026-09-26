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


def row_branch_ids(obj) -> list:
    """The branches one academic or calendar row belongs to; empty means shared.

    Read from the row's own branch where it has one, and from its parent where
    it does not. A session is judged by the branches it covers, a term by its
    session, an exam by its exam period, and a timetable slot or an exam paper
    by its class - so Ikeja can put its own classes' papers into a school-wide
    exam week without being able to rename or publish the exam itself.
    """
    if hasattr(obj, "branch_links"):
        if getattr(obj, "is_school_wide", False):
            return []
        return [link.branch_id for link in obj.branch_links.all()]
    if hasattr(obj, "branch_id"):
        return [obj.branch_id] if obj.branch_id is not None else []
    if getattr(obj, "school_class_id", None) is not None:
        branch_id = obj.school_class.branch_id
        return [branch_id] if branch_id is not None else []
    if getattr(obj, "calendar_event_id", None) is not None:
        branch_id = obj.calendar_event.branch_id
        return [branch_id] if branch_id is not None else []
    if getattr(obj, "session_id", None) is not None:
        # A term: no branch of its own, so its year decides.
        return row_branch_ids(obj.session)
    return []


def row_visible_to(user, tenant, obj) -> bool:
    """The inclusive read, for one row already resolved by primary key.

    A shared row, or a row with at least one branch the caller works in.
    """
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return True
    ids = row_branch_ids(obj)
    return not ids or bool(set(ids) & visible)


def assert_may_change(user, tenant, obj, *, message: str = "") -> None:
    """Refuse a write to a row the caller may read but not change (403).

    Shared rows are read-only to a branch-bound caller; see
    :func:`vs_rbac.scoping.caller_may_change`.
    """
    from vs_rbac.scoping import assert_caller_may_change

    assert_caller_may_change(user, tenant, row_branch_ids(obj), message=message)


def add_manage_flag(serializer, instance, data):
    """Put ``can_manage`` on a serialized row when a request is in context.

    Whether the viewer may change the row rather than only read it, so a screen
    can hide the controls the server would refuse. Absent where a serializer
    runs without a request (a write response built with a bare context); a
    screen reads a missing flag as manageable. ``visible_branch_ids`` is
    memoised on the user, so a page of rows costs no query per row.
    """
    request = serializer.context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return data
    from vs_rbac.scoping import caller_may_change

    data["can_manage"] = caller_may_change(
        user, getattr(request, "tenant", None), row_branch_ids(instance),
    )
    return data


def guard_detail(view, obj):
    """The read and write rules for a row a detail view resolved by pk.

    Another branch's row answers 404, exactly like an unknown id; a shared row
    answers 403 to a write from a branch-bound caller.
    """
    from rest_framework.exceptions import NotFound
    from rest_framework.permissions import SAFE_METHODS

    user, tenant = view.request.user, view.tenant
    if not row_visible_to(user, tenant, obj):
        raise NotFound("No such record at this school.")
    if view.request.method not in SAFE_METHODS:
        assert_may_change(user, tenant, obj)


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
            # 403 rather than 422: a shared row speaks for branches they do
            # not work in, so this is about who they are, not what they typed.
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
