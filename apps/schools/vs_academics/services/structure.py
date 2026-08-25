"""Departments, programmes and levels: codes, containment and progression.

Three rules live here rather than in a serializer, because each of them is
about a row's relationship to something else and a per-field validator cannot
see it.
"""
from __future__ import annotations

import re

from django.db import transaction

from ..exceptions import DuplicateInBatch, LevelCrossProgram, LevelCycle
from ..models import Level


def generate_code(name, taken, *, length=3, max_length=20):
    """A code built from a name, made unique by suffixing.

    *taken* is the set of codes already used for this kind of thing in this
    tenant, lowercased. Uniqueness is per kind: a department called Languages
    does not stop a programme being called Languages, so the caller passes only
    its own model's codes.
    """
    base = re.sub(r"[^A-Za-z0-9]", "", name or "").upper()[:length] or "GEN"
    base = base[:max_length]
    if base.lower() not in taken:
        return base
    for n in range(2, 1000):
        suffix = str(n)
        candidate = f"{base[:max_length - len(suffix)]}{suffix}"
        if candidate.lower() not in taken:
            return candidate
    raise DuplicateInBatch(f"Could not build a unique code from {name!r}.")


def plan_bulk_levels(program, names, existing_codes):
    """Work out what a bulk level create would write, refusing the whole batch.

    A duplicate anywhere - against an existing level or against another entry
    in the same list - fails the call and creates nothing, naming every
    offender. Half-creating a run of levels is worse than creating none:
    the school cannot tell which of the names it typed took.
    """
    cleaned = [n.strip() for n in names if n and n.strip()]
    existing_names = {
        n.lower() for n in Level.all_objects
        .filter(program=program).values_list("name", flat=True)
    }

    offenders, seen = [], set()
    for name in cleaned:
        key = name.lower()
        if key in existing_names or key in seen:
            offenders.append(name)
        seen.add(key)
    if offenders:
        raise DuplicateInBatch(
            "Some of these levels already exist in this programme.",
            names=offenders,
        )

    start = (
        Level.all_objects.filter(program=program)
        .order_by("-order_index").values_list("order_index", flat=True).first()
        or 0
    )
    taken = set(existing_codes)
    plan = []
    for offset, name in enumerate(cleaned, start=1):
        code = generate_code(name, taken)
        taken.add(code.lower())
        plan.append({
            "name": name, "code": code, "order_index": start + offset,
        })
    return plan


def assert_promotion_target(level, target, *, cross_program=False):
    """Refuse a promotion edge that cannot mean anything.

    Four cases, and the last is the one version 1.0 of the FRD specified
    nowhere. No consumer exists yet to be hurt by an infinite promotion loop,
    which is exactly why it is closed now rather than after a school has a
    term of data in it.
    """
    if target is None:
        return
    if target.pk == level.pk:
        raise LevelCycle(
            f"{level.name} cannot promote into itself.",
            level=level.name,
        )
    if target.program_id != level.program_id and not cross_program:
        raise LevelCrossProgram(
            f"{target.name} belongs to a different programme. Confirm the "
            f"move if you meant it.",
            level=level.name, target=target.name,
        )
    # A shared level may not promote into one branch's level: that would send
    # every branch's pupils to one site. The other direction is fine, and is
    # how a branch running only junior secondary hands its pupils on.
    if level.branch_id is None and target.branch_id is not None:
        from ..exceptions import BranchScopeConflict

        raise BranchScopeConflict(
            f"{level.name} belongs to the whole school, so it cannot promote "
            f"into {target.name}, which belongs to one branch only.",
            level=level.name, target=target.name,
        )
    # Walk the chain from the target. If we arrive back at the source, the edge
    # would close a loop and promotion would never terminate.
    seen, cursor = {target.pk}, target
    while cursor.next_level_id is not None:
        if cursor.next_level_id == level.pk:
            raise LevelCycle(
                f"{level.name} already comes after {target.name}, so this "
                f"would make them promote in a loop.",
                level=level.name, target=target.name,
            )
        if cursor.next_level_id in seen:
            break                       # a pre-existing loop, not one we made
        seen.add(cursor.next_level_id)
        cursor = Level.all_objects.get(pk=cursor.next_level_id)
