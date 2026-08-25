"""The two composed reads: the structure tree, and the overview.

Both are assembled in Python from a fixed number of flat queries rather than by
walking relations, and that is the whole design. A tree built by following
``program.levels`` then ``level.classes`` costs one query per parent and looks
fine on the four programmes a developer seeds; a school with forty levels and
three hundred classes pays for every one of them on a screen that is meant to
be an overview.

So: one query per level of the tree, joined up by dictionary lookup. Six at
full depth, not six per programme.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Count, Q

from ..models import (
    AcademicSession,
    Department,
    Level,
    Program,
    SchoolClass,
    SessionStatus,
    Subject,
    SubjectOffering,
)
from .scoping import scope_to_visible_branches


def _scoped(model, user, tenant, **filters):
    return scope_to_visible_branches(
        model.objects.filter(tenant=tenant, **filters), user, tenant,
    )


def _scope_cell(row, multi_branch):
    """The chip a row shows, or nothing at all where there is one branch."""
    if not multi_branch:
        return {}
    return {
        "scope_label": row.branch.name if row.branch_id else "School-wide",
        "is_shared": row.branch_id is None,
    }


def build_tree(user, tenant, *, session=None, branch=None, full=False,
               multi_branch=True):
    """Session, programmes, levels, and at full depth classes and subjects.

    ``branch`` narrows the whole tree rather than the counts alone: a programme
    belonging to another branch does not appear at all, while a shared one
    appears with its counts narrowed.
    """
    programs = _scoped(Program, user, tenant, is_active=True)
    levels = _scoped(Level, user, tenant, is_active=True)
    classes = _scoped(SchoolClass, user, tenant, is_active=True)

    if branch is not None:
        narrow = Q(branch__isnull=True) | Q(branch=branch)
        programs, levels, classes = (
            programs.filter(narrow), levels.filter(narrow), classes.filter(narrow),
        )

    programs = list(programs.select_related("branch").order_by("order_index", "name"))
    levels = list(
        levels.select_related("branch").order_by("program_id", "order_index"),
    )
    level_ids = [level.id for level in levels]

    classes = list(
        classes.select_related("branch")
        .filter(level_id__in=level_ids).order_by("level_id", "name"),
    ) if full else []

    offerings = list(
        SubjectOffering.objects
        .filter(tenant=tenant, level_id__in=level_ids)
        .select_related("subject", "subject__branch")
        .order_by("level_id", "subject__name"),
    ) if full else []

    # One aggregate each, rather than a count per parent.
    class_counts = dict(
        _scoped(SchoolClass, user, tenant, is_active=True)
        .filter(level_id__in=level_ids)
        .values_list("level_id")
        .annotate(n=Count("id")).values_list("level_id", "n"),
    )
    subject_counts = dict(
        SubjectOffering.objects
        .filter(tenant=tenant, level_id__in=level_ids)
        .values_list("level_id")
        .annotate(n=Count("id")).values_list("level_id", "n"),
    )

    by_program, by_level, subjects_by_level = {}, {}, {}
    for level in levels:
        by_program.setdefault(level.program_id, []).append(level)
    for klass in classes:
        by_level.setdefault(klass.level_id, []).append(klass)
    for offering in offerings:
        subjects_by_level.setdefault(offering.level_id, []).append(offering)

    rows = []
    root_label = session.name if session else "Academic structure"
    rows.append({
        "id": "session", "kind": "Session", "label": root_label, "depth": 0,
        "contains": _plural(len(programs), "programme"),
    })

    for program in programs:
        kids = by_program.get(program.id, [])
        rows.append({
            "id": f"p:{program.id}", "kind": "Programme", "label": program.name,
            "depth": 1, "contains": _plural(len(kids), "level"),
            **_scope_cell(program, multi_branch),
        })
        for level in kids:
            n_classes = class_counts.get(level.id, 0)
            rows.append({
                "id": f"l:{level.id}", "kind": "Level", "label": level.name,
                "depth": 2,
                "contains": _plural(n_classes, "class", plural="classes"),
                "class_count": n_classes,
                "subject_count": subject_counts.get(level.id, 0),
                **_scope_cell(level, multi_branch),
            })
            if not full:
                continue
            for klass in by_level.get(level.id, []):
                offered = subjects_by_level.get(level.id, [])
                label = f"{klass.name} · arm {klass.arm}" if klass.arm else klass.name
                rows.append({
                    "id": f"c:{klass.id}", "kind": "Class", "label": label,
                    "depth": 3, "contains": _plural(len(offered), "subject"),
                    **_scope_cell(klass, multi_branch),
                })
                for offering in offered:
                    subject = offering.subject
                    core = offering.is_core if offering.is_core is not None else subject.is_core
                    rows.append({
                        "id": f"c:{klass.id}:s:{subject.id}", "kind": "Subject",
                        "label": subject.name, "depth": 4,
                        "contains": "Core" if core else "Elective",
                        **_scope_cell(subject, multi_branch),
                    })
    return rows


def _plural(n, word, plural=None):
    if n == 0:
        return f"No {plural or word + 's'}"
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def term_state(term, today):
    if term.archived_at is not None:
        return "archived"
    if term.end_date < today:
        return "completed"
    if term.start_date <= today:
        return "ongoing"
    return "pending"


def build_overview(user, tenant, *, today=None, multi_branch=True, branch=None):
    """The landing screen: the live year, and what the school has built.

    One call because it is one screen. Composing it from the five list
    endpoints would make a page of numbers cost five round trips, and each of
    those lists would then be paginated for no reason.

    ``branch`` narrows the counts the way it narrows the tree beside them: a
    shared row counts everywhere, a row belonging to another branch counts
    nowhere. Without it the screen showed a branch filter above four numbers
    that ignored it, which reads as a broken filter rather than as a total.

    The SESSION block is deliberately not narrowed. A school runs one live year
    and the hero states which; filtering it by branch would blank the hero for a
    branch whose year names other branches, which is a different fact and one
    ``branches_without_a_session`` already reports.
    """
    today = today or dt.date.today()

    active = (
        AcademicSession.objects
        .filter(tenant=tenant, status=SessionStatus.ACTIVE)
        .prefetch_related("terms").first()
    )
    session_block = None
    if active is not None:
        terms = list(active.terms.all())
        total = max((active.end_date - active.start_date).days, 1)
        elapsed = min(max((today - active.start_date).days, 0), total)
        current = next(
            (t for t in terms if term_state(t, today) == "ongoing"), None,
        )
        upcoming = next(
            (t for t in terms if term_state(t, today) == "pending"), None,
        )
        session_block = {
            "id": active.id,
            "name": active.name,
            "start_date": active.start_date,
            "end_date": active.end_date,
            "percent_elapsed": round(elapsed / total * 100),
            "current_term": current.name if current else None,
            "next_term": upcoming.name if upcoming else None,
            "terms": [
                {
                    "id": t.id, "name": t.name, "order_index": t.order_index,
                    "start_date": t.start_date, "end_date": t.end_date,
                    # Derived from today's date and never stored, so it cannot
                    # drift from the dates it describes.
                    "state": term_state(t, today),
                }
                for t in terms
            ],
        }

    def _count(model):
        qs = _scoped(model, user, tenant, is_active=True)
        if branch is not None:
            # A shared row belongs to this branch too - that is what a null
            # branch MEANS - so it is counted, not excluded.
            qs = qs.filter(Q(branch__isnull=True) | Q(branch=branch))
        return qs.count()

    counts = {
        "sessions": AcademicSession.objects.filter(tenant=tenant).count(),
        "departments": _count(Department),
        "programs": _count(Program),
        "levels": _count(Level),
        "classes": _count(SchoolClass),
        "subjects": _count(Subject),
    }

    return {
        "active_session": session_block,
        "counts": counts,
        "branches_without_a_session": (
            _branches_without_a_session(tenant) if multi_branch else []
        ),
    }


def _branches_without_a_session(tenant):
    """Branches in no live year at all.

    Reachable only once a school has split its calendar: while a year names no
    branches it covers every branch the school has, including ones opened
    afterwards. Once one names its branches explicitly, a branch created later
    is in nothing, and there is no correct year to guess for it. So it is
    reported here rather than defaulted, because it is a question only the
    school can answer.
    """
    from vs_tenants.models import Branch
    from ..models import SessionBranch

    live = AcademicSession.objects.filter(tenant=tenant, status=SessionStatus.ACTIVE)
    if live.filter(is_school_wide=True).exists():
        return []                       # a school-wide year covers everything
    covered = set(
        SessionBranch.objects
        .filter(tenant=tenant, session_status=SessionStatus.ACTIVE)
        .values_list("branch_id", flat=True)
    )
    return [
        {"id": b.id, "name": b.name}
        for b in Branch.all_objects.filter(tenant=tenant).order_by("code")
        if b.id not in covered
    ]
