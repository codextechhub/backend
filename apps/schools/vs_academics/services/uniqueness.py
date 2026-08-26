"""Duplicate names and codes, refused with a sentence a school can act on.

The database already refuses these - every model here carries a case-insensitive
unique constraint - but an ``IntegrityError`` reaches the caller as
``core.exceptions``' generic "A record with these details already exists", which
names no field, no row and no branch. On a drawer with a Name box and a Code box
that is a refusal the person cannot act on: they do not know which of the two
was wrong, let alone what it collided with.

So the check is made here, before the write, and the message says what was hit
and where. The rule the message states is the rule the constraint enforces, and
they differ by kind - a department name is unique per SCHOOL, a level name only
within its PROGRAMME - so the caller passes the scope it is writing into rather
than this module guessing.

**The branch is named even when the caller cannot see it.** A Lekki-tied admin
who picks a code already used at Ikeja is told so, because the alternative is a
refusal they cannot resolve and cannot even understand: their own list does not
contain the row, `scope_to_visible_branches` having removed it. What is disclosed
is one row's name and branch inside the caller's OWN school, to somebody the
school made an administrator.

This module is not a replacement for the constraints. It runs inside the same
transaction as the write it guards, but two concurrent requests can still both
pass it, and the database is what stops the second one. That path still answers
the generic message, and that is the correct trade: it is a race, not a typo.
"""
from __future__ import annotations

from django.db.models.functions import Lower

from ..exceptions import DuplicateCode, DuplicateName


def _scope_of(row, multi_branch):
    """How to describe where the clashing row lives.

    Returns None for "the whole school", which includes every row at a school
    with one branch: naming its only branch would be noise, and the recede rule
    the serializers follow applies to refusals too.
    """
    if not multi_branch:
        return None
    branch = getattr(row, "branch", None)
    return branch.name if branch is not None else None


def _name_message(value, row, *, branch_label, writing_to_branch, within):
    if within:
        # The constraint is scoped to the parent, so the message must be:
        # "JSS1 already exists" is false at a school running it in two.
        where = f" at {branch_label}" if branch_label else ""
        return (
            f"{value} already exists in {within}{where}. "
            f"Pick a different name."
        )
    if branch_label:
        return (
            f"{value} already exists at {branch_label}. Names are unique across "
            f"the whole school, so pick a different one."
        )
    if writing_to_branch:
        return (
            f"{value} already exists school-wide, so this branch already has it. "
            f"Use the one that exists, or give this one its own name and code."
        )
    return f"{value} already exists in this school. Pick a different name."


def _code_message(row, *, branch_label, within):
    where = (
        f" at {branch_label}" if branch_label
        else "" if within else ", which is school-wide"
    )
    scope_rule = (
        f"Codes are unique within {within}, so pick a different one." if within
        else "Codes are unique across the whole school, so pick a different one."
    )
    return f"That code belongs to {row.name}{where}. {scope_rule}"


def assert_unique(
    queryset,
    *,
    name=None,
    code=None,
    exclude_pk=None,
    multi_branch=True,
    within=None,
    writing_to_branch=False,
):
    """Refuse a duplicate name or code before writing it.

    *queryset* is already narrowed to the scope the constraint uses: every row
    of this kind in the tenant, or - for a level or a class - only those inside
    the parent. Pass ``all_objects`` so an archived row still blocks the name it
    holds; the constraint does not exempt it either.

    *within* is the parent's name where uniqueness is scoped to one
    ("Junior Secondary"), and None where it is school-wide.

    *writing_to_branch* changes only the wording, for the case worth spelling
    out: somebody making a branch copy of something the whole school already
    has does not need a different name, they need to stop.

    Checks the name first, because that is the field the person filled in first
    and a screen shows one message at a time.
    """
    base = queryset.exclude(pk=exclude_pk) if exclude_pk else queryset

    if name and str(name).strip():
        value = str(name).strip()
        hit = base.annotate(_n=Lower("name")).filter(_n=value.lower()).first()
        if hit is not None:
            branch_label = _scope_of(hit, multi_branch)
            raise DuplicateName(
                _name_message(
                    value, hit,
                    branch_label=branch_label,
                    writing_to_branch=writing_to_branch,
                    within=within,
                ),
                field="name",
                conflict=hit.name,
                scope_label=branch_label or "School-wide",
            )

    if code and str(code).strip():
        value = str(code).strip()
        hit = base.annotate(_c=Lower("code")).filter(_c=value.lower()).first()
        if hit is not None:
            branch_label = _scope_of(hit, multi_branch)
            raise DuplicateCode(
                _code_message(hit, branch_label=branch_label, within=within),
                field="code",
                conflict=hit.name,
                scope_label=branch_label or "School-wide",
            )
