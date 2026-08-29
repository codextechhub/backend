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


# ── The lens ─────────────────────────────────────────────────────────────────
#
# `scope_to_visible_branches` above answers "which rows MAY this caller see".
# This answers a different question: "which branch is the caller LOOKING AT".
# The first is security and is never optional; the second is a control at the
# top of the screen and applies only when the caller has set it.
#
# They were confused, and the cost was not small. Every screen in this module
# sends `?branch=<id>` from the switcher, and only rooms and the bell schedule
# ever read it. An unrecognised query parameter is not an error in DRF, so the
# other five surfaces accepted the parameter, ignored it, and answered with
# every branch: a Lekki administrator switched to Lekki was shown Ikeja's
# events, Ikeja's class timetables and Ikeja's exam papers, with the switcher
# on screen saying Lekki the whole time.
#
# So the lens lives here, once, and every list read calls it. A surface added
# later that forgets to is a surface that does not filter, which is why the
# module's tests now assert the lens on each of them by name.

def lens_branch(view):
    """The branch this request is looking THROUGH, or None for all of them.

    None means "every branch I may see", which is the honest default and what a
    school administrator wants. A single-branch school never sends the
    parameter and would mean nothing by it if it did, so the lens is ignored
    there rather than being made to do nothing in a more expensive way.
    """
    raw = str(view.request.query_params.get("branch") or "").strip()
    if not raw or raw.lower() == "all" or not view.multi_branch:
        return None
    return resolve_branch_reference(view.tenant, raw, "branch")


def narrow_to_lens(qs, branch, *, field="branch"):
    """Narrow to one branch's rows AND the school's shared ones.

    **Inclusive, and that is the whole point.** A null branch means "shared
    across the whole school" and never "no branch was chosen", so a school-wide
    public holiday belongs on Lekki's calendar as much as on Ikeja's. An
    exclusive read would empty the screen of exactly the rows most schools
    create, which is the same mistake as not filtering at all, made in the
    opposite direction.

    Rooms are the exception and do not use this: a room is a physical place
    with a non-null branch, so there is no shared row to include.
    """
    if branch is None:
        return qs
    from django.db.models import Q as _Q

    return qs.filter(
        _Q(**{field: branch}) | _Q(**{f"{field}__isnull": True}),
    )
