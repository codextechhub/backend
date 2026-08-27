"""Which rows a caller sees, and which branch a row they write belongs to.

Deliberately thin: ``vs_academics.services.scoping`` already answers both
questions the same way for the same kind of data, and a second implementation
would drift. This module re-exports its two functions and adds the one rule the
academic catalogue does not have, which is Room's non-null branch.

The read is **inclusive**: a row with no branch is shared by the whole school
and everybody sees it. That is right for a calendar for the same reason it is
right for a catalogue - the shared rows are most of it, and filtering them out
leaves a branch admin with an empty screen whenever the school published at
school level, which is the normal case. ``vs_procurement`` takes the opposite
reading for its documents and is right to.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied, ValidationError

from schools.vs_academics.services.scoping import (  # noqa: F401  (re-exported)
    UNSET,
    branch_dimension_applies,
    raised_branch,
    scope_to_visible_branches,
)
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
from vs_tenants.references import resolve_branch_reference


def room_branch(user, tenant, requested=UNSET, *, field="branch"):
    """The branch a room this caller creates belongs to. Never None.

    ``raised_branch`` answers "the whole school" with None, which is a legal
    and common answer for an event or a period and is never a legal answer for
    a room. So this wraps it and closes the one gap: where a caller could have
    written a shared row, a room instead falls back to the school's only branch
    if there is exactly one, and otherwise has to be told which.

    That fallback is what makes the design's single-branch school work. There
    the branch question is never asked on screen, so nothing is sent, and the
    room still has to land somewhere.
    """
    explicit_school_wide = requested is None
    if explicit_school_wide:
        raise ValidationError({
            field: (
                "A room is a physical place, so it belongs to one branch. "
                "Say which one."
            ),
        })

    branch = raised_branch(user, tenant, requested, field=field)
    if branch is not None:
        return branch

    # The caller is not narrowed to any branch and named none. In a
    # single-branch school that is the ordinary case and the answer is obvious.
    from vs_tenants.models import Branch

    branches = list(Branch.all_objects.filter(tenant=tenant).order_by("pk")[:2])
    if len(branches) == 1:
        return branches[0]
    raise ValidationError({
        field: "Say which branch this room is at.",
    })


def visible_branch_id_set(user, tenant):
    """The caller's branches as a set of ids, or ``WHOLE_TENANT``.

    Used by the clash reporter to decide whether it may name the other side of
    a clash. Kept here so that the one authority on the question -
    ``vs_rbac.scoping.visible_branch_ids`` - is reached through one import in
    this module rather than five.
    """
    return visible_branch_ids(user, tenant)


def can_see_branch(visible, branch_id) -> bool:
    """Whether a caller with *visible* may be told about a row at *branch_id*.

    A null branch is shared by the whole school and is always visible.
    """
    if branch_id is None:
        return True
    if visible is WHOLE_TENANT:
        return True
    return branch_id in visible
