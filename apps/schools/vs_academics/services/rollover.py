"""Start a year from the one before it.

Levels, classes and subjects belong to a year, which is honest and would be
punishing on its own: a school that has just created 2027/2028 would face
sixteen levels, eight classes and seven subjects to retype, all of them the same
as last year's. So a new year is SEEDED from an existing one, and the school
edits the differences.

What is copied and what is not:

* **Levels, with their promotion links.** JSS1 promotes to JSS2 in the new year
  the way it did in the old one - re-pointed at the new rows, never left aimed
  at last year's.
* **Classes**, keeping their level, branch, arm and capacity. Not their
  ENROLMENT: who sat in JSS1 A last year is last year's fact, and is exactly
  what carrying the structure over is meant to stop overwriting.
* **Subjects and their offerings**, so the curriculum arrives intact.
* **Nothing archived.** An archived level or class was withdrawn on purpose;
  copying it forward would undo that decision silently.

Departments and programmes are not copied because they are not per-year: the
new levels point at the same programmes the old ones did.

Idempotent by refusal rather than by merging: a target year that already holds
structure is refused, because "top it up" and "copy it again" are different
intentions and merging silently picks one.
"""
from __future__ import annotations

from django.db import transaction

from ..exceptions import AcademicsError
from .years import assert_year_is_writable


class TargetYearNotEmpty(AcademicsError):
    """The year being copied into already has structure of its own."""

    error_code = "TARGET_YEAR_NOT_EMPTY"
    http_status = 409


class NothingToCopy(AcademicsError):
    error_code = "NOTHING_TO_COPY"
    http_status = 422


@transaction.atomic
def roll_forward(tenant, *, source, target):
    """Copy one year's structure into another. Returns what it wrote."""
    from ..models import Level, SchoolClass, Subject, SubjectOffering

    if source.pk == target.pk:
        raise NothingToCopy("Pick a different year to copy from.")

    # Not into a year that has closed. Everything else in the module refuses a
    # write into an archived year; a copy is eleven hundred writes at once.
    assert_year_is_writable(target)

    # Every kind, not just levels. A year seeded with subjects and no levels
    # passed a levels-only check and got a second copy of each - and where the
    # names matched, the unique constraint answered for us in SQL.
    started = {
        "level": Level.all_objects.filter(tenant=tenant, session=target).count(),
        "class": SchoolClass.all_objects.filter(
            tenant=tenant, session=target,
        ).count(),
        "subject": Subject.all_objects.filter(
            tenant=tenant, session=target,
        ).count(),
    }
    held = [(n, kind) for kind, n in started.items() if n]
    if held:
        what = ", ".join(
            f"{n} {kind if n == 1 else kind + 's'}" for n, kind in held
        )
        raise TargetYearNotEmpty(
            f"{target.name} already has {what}. Copying into a year that has "
            f"been started would double what is there - clear it first, or "
            f"pick an empty year.",
        )

    levels = list(
        Level.all_objects.filter(tenant=tenant, session=source, is_active=True)
        .order_by("program_id", "order_index")
    )
    subjects = list(
        Subject.all_objects.filter(tenant=tenant, session=source, is_active=True)
    )
    if not levels and not subjects:
        raise NothingToCopy(
            f"{source.name} has no levels or subjects to copy.",
        )

    # ── Levels ────────────────────────────────────────────────────────────
    # Two passes: the rows first, then the promotion links, because a level
    # cannot point at a sibling that does not exist yet.
    level_map = {}
    for old in levels:
        new = Level.objects.create(
            tenant=tenant, session=target, program_id=old.program_id,
            branch_id=old.branch_id, name=old.name, code=old.code,
            description=old.description, order_index=old.order_index,
        )
        level_map[old.pk] = new

    for old in levels:
        if old.next_level_id and old.next_level_id in level_map:
            new = level_map[old.pk]
            new.next_level = level_map[old.next_level_id]
            new.save(update_fields=["next_level", "updated_at"])

    # ── Classes ───────────────────────────────────────────────────────────
    classes = list(
        SchoolClass.all_objects.filter(
            tenant=tenant, session=source, is_active=True,
        )
    )
    SchoolClass.objects.bulk_create([
        SchoolClass(
            tenant=tenant, session=target, level=level_map[old.level_id],
            branch_id=old.branch_id, name=old.name, code=old.code,
            description=old.description, arm=old.arm, capacity=old.capacity,
            created_by_id=old.created_by_id,
        )
        for old in classes if old.level_id in level_map
    ])

    # ── Subjects, and where they are taught ───────────────────────────────
    subject_map = {}
    for old in subjects:
        subject_map[old.pk] = Subject.objects.create(
            tenant=tenant, session=target, branch_id=old.branch_id,
            department_id=old.department_id, name=old.name, code=old.code,
            description=old.description, is_core=old.is_core,
        )

    offerings = SubjectOffering.all_objects.filter(
        tenant=tenant, subject__in=[o.pk for o in subjects],
    )
    SubjectOffering.objects.bulk_create([
        SubjectOffering(
            tenant=tenant, subject=subject_map[o.subject_id],
            level=level_map[o.level_id], is_core=o.is_core,
        )
        for o in offerings
        if o.subject_id in subject_map and o.level_id in level_map
    ])

    return {
        "levels": len(level_map),
        "classes": len(classes),
        "subjects": len(subject_map),
    }
