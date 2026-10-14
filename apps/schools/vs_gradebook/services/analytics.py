"""Performance analytics: how a term's published results read across subjects, classes and students.

**Published results only.** Everything here is read from the snapshots a class
published, and from nothing a teacher is still entering, so a class that is
awaiting the principal contributes nothing however complete its sheets are. A
student's figures are those of their *latest* snapshot, which is the version an
approved amendment left on the result, not the one first issued. A student whose
result is held is counted: published is published, and the hold only keeps the
card from the family.

**Scope.** The classes of the term's school year the caller can see, exactly as
the results hub reaches them (:func:`.hubs.reachable_classes`), narrowed by
``branch``. A branch-bound reader therefore compares a subject only across the
classes they can see. A class with no branch at a school with several has no
place for results to live and is not here.

**Nothing here judges a teacher.** No teacher is named anywhere in the payload,
and a gap between two classes' averages in the same subject is a question for the
head of department, not a statement about whoever taught either class.

The payload, before Field Access is applied:

* ``subjects``: one row for each subject of each published class. ``average`` is
  the mean of the published subject percentages to two places, ``spread`` the
  count of students on each grade in the class's own scale order (a grade nobody
  earned is there with 0), ``pass_rate`` the whole percentage of marked students
  outside a fail band and ``marked`` the students with a percentage. Ordered by
  subject name, then class name.
* ``compare``: a subject offered by two or more published classes with an
  average, with each class's average and the ``gap`` between the highest and
  lowest (two places). Classes appear in level order, so the order says nothing
  about who is ahead.
* ``movers``: a student's overall average against their most recent earlier
  published term that has one, in any class and any school year, "earlier" meaning
  the term starts before this one. The biggest fall comes first. A student with no
  earlier published result is not listed.
* ``tops``: the highest overall average of each class, every student on it named
  when several tie to two places. Present only when the school ranks
  (``gradebook.rank_in_class``) and for a class whose snapshots carry positions,
  so the screen never shows a "top of class" the report cards do not.

Every figure is a string of two decimal places, as the snapshots carry them.
The whole report is read in a fixed number of queries however many classes the
caller can see.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..constants import CFG_RANK_IN_CLASS, StoredStage
from ..models import ClassTerm, StudentTermResult
from . import compute, hubs
from .classbook import percentage_of
from .rules import read_gradebook_rules

_HUNDRED = Decimal(100)
_WHOLE = Decimal(1)


def _ref(record) -> dict:
    return {"id": record.pk, "name": record.name}


def _signed(change: Decimal) -> str:
    """*change* to two places with its sign: ``+2.00``, ``-3.50``, ``0.00``."""
    shown = compute.round2(change)
    return f"+{shown}" if shown > 0 else str(shown)


# ── Reading the published snapshots ────────────────────────────────────────

def published_class_ids(tenant, term, class_ids) -> set:
    """The ids of *class_ids* whose class-term is published for *term*."""
    return set(
        ClassTerm.objects.filter(
            tenant=tenant, term=term, stage=StoredStage.PUBLISHED, school_class_id__in=class_ids,
        ).values_list("school_class_id", flat=True)
    )


def latest_snapshots(tenant, term, class_ids) -> dict:
    """``{class_id: [snapshot, ...]}`` for every published result of *term* in *class_ids*.

    One query. A snapshot is the version the result row carries now.
    """
    out: dict = {}
    rows = StudentTermResult.objects.filter(
        tenant=tenant, class_term__term=term, class_term__stage=StoredStage.PUBLISHED,
        class_term__school_class_id__in=class_ids, snapshot__isnull=False,
    ).values_list("class_term__school_class_id", "snapshot")
    for class_id, snapshot in rows:
        out.setdefault(class_id, []).append(snapshot)
    return out


# ── By subject and class ───────────────────────────────────────────────────

def _subject_rows(snapshots_by_class, classes) -> list:
    """One row for each subject of each published class, with its figures."""
    rows = []
    for school_class in classes:
        snapshots = snapshots_by_class.get(school_class.pk, [])
        if not snapshots:
            continue
        order = [band["grade"] for band in snapshots[0].get("grading_key", [])]
        per_subject: dict = {}
        for snapshot in snapshots:
            for line in snapshot["subjects"]:
                subject = line["subject"]
                bucket = per_subject.setdefault(
                    subject["id"], {"subject": subject, "percentages": [], "grades": {}, "passes": 0},
                )
                if line["percentage"] is None:
                    continue
                bucket["percentages"].append(Decimal(line["percentage"]))
                grade = line["grade"]
                bucket["grades"][grade] = bucket["grades"].get(grade, 0) + 1
                if not line["is_fail"]:
                    bucket["passes"] += 1
        for bucket in per_subject.values():
            marked = len(bucket["percentages"])
            grades = bucket["grades"]
            names = order + [name for name in grades if name not in order]
            rows.append({
                "subject": {"id": bucket["subject"]["id"], "name": bucket["subject"]["name"]},
                "class": _ref(school_class),
                "average": percentage_of(compute.overall_average(bucket["percentages"])),
                "spread": [{"grade": name, "count": grades.get(name, 0)} for name in names],
                "pass_rate": (
                    int((Decimal(bucket["passes"]) * _HUNDRED / Decimal(marked)).quantize(
                        _WHOLE, rounding=ROUND_HALF_UP,
                    )) if marked else None
                ),
                "marked": marked,
            })
    rows.sort(key=lambda r: (
        r["subject"]["name"].casefold(), r["class"]["name"].casefold(), r["class"]["id"],
    ))
    return rows


def _compare(subject_rows, class_order) -> list:
    """Each subject two or more published classes have an average for, with the gap."""
    by_subject: dict = {}
    for row in subject_rows:
        if row["average"] is not None:
            by_subject.setdefault(row["subject"]["id"], []).append(row)
    out = []
    for rows in by_subject.values():
        if len(rows) < 2:
            continue
        figures = [Decimal(row["average"]) for row in rows]
        arms = sorted(rows, key=lambda r: class_order[r["class"]["id"]])
        out.append({
            "subject": rows[0]["subject"],
            "arms": [{"class": row["class"], "average": row["average"]} for row in arms],
            "gap": str(compute.round2(max(figures) - min(figures))),
        })
    out.sort(key=lambda r: (r["subject"]["name"].casefold(), r["subject"]["id"]))
    return out


# ── Term on term ───────────────────────────────────────────────────────────

def _earlier_averages(tenant, term, student_ids) -> dict:
    """``{student_id: (term id, term name, overall average)}`` against their most recent earlier term.

    The most recent published term that began before *term* and carries an
    overall average; a published term with none (every subject excused) gives
    nothing to compare and the one before it is used. One query.
    """
    found: dict = {}
    rows = (
        StudentTermResult.objects.filter(
            tenant=tenant, student_id__in=student_ids, snapshot__isnull=False,
            class_term__stage=StoredStage.PUBLISHED,
            class_term__term__start_date__lt=term.start_date,
        )
        .order_by("-class_term__term__start_date", "-class_term__published_at", "-pk")
        .values_list(
            "student_id", "class_term__term_id", "class_term__term__name",
            "snapshot__overall_average",
        )
    )
    for student_id, term_id, term_name, average in rows:
        if average is not None and student_id not in found:
            found[student_id] = (term_id, term_name, average)
    return found


def _movers(tenant, term, snapshots_by_class) -> list:
    """Students whose overall average moved against their last earlier published term."""
    current = [
        snapshot
        for snapshots in snapshots_by_class.values() for snapshot in snapshots
        if snapshot.get("overall_average") is not None
    ]
    earlier = _earlier_averages(tenant, term, {s["student"]["id"] for s in current})
    rows = []
    for snapshot in current:
        before = earlier.get(snapshot["student"]["id"])
        if before is None:
            continue
        term_id, term_name, then = before
        now = snapshot["overall_average"]
        rows.append({
            "student": dict(snapshot["student"]),
            "class": dict(snapshot["class"]),
            "from_term": {"id": term_id, "name": term_name},
            "from": then,
            "to": now,
            "change": _signed(Decimal(now) - Decimal(then)),
        })
    rows.sort(key=lambda r: (
        Decimal(r["change"]), r["student"]["name"].casefold(), r["student"]["id"],
    ))
    return rows


# ── The top of each class ──────────────────────────────────────────────────

def _tops(snapshots_by_class, classes) -> list:
    """The highest overall average in each class that ranks, with every student on it."""
    out = []
    for school_class in classes:
        snapshots = snapshots_by_class.get(school_class.pk, [])
        ranked = [s for s in snapshots if "position" in s and s.get("overall_average") is not None]
        if not ranked:
            continue
        best = max(compute.round2(Decimal(s["overall_average"])) for s in ranked)
        students = sorted(
            (
                {"id": s["student"]["id"], "name": s["student"]["name"]}
                for s in ranked if compute.round2(Decimal(s["overall_average"])) == best
            ),
            key=lambda student: (student["name"].casefold(), student["id"]),
        )
        out.append({"class": _ref(school_class), "students": students, "average": str(best)})
    return out


# ── The report ─────────────────────────────────────────────────────────────

def report(tenant, user, term, branch) -> dict:
    """The analytics for *term* in the caller's branches, before Field Access is applied."""
    placed, _unplaced, branches, _visible = hubs.reachable_classes(tenant, user, term, branch)
    several = len(branches) > 1
    published = published_class_ids(tenant, term, [c.pk for c, _ in placed])
    placed = [(c, home) for c, home in placed if c.pk in published]
    classes = [school_class for school_class, _ in placed]
    snapshots = latest_snapshots(tenant, term, [c.pk for c in classes])

    class_order = {school_class.pk: index for index, school_class in enumerate(classes)}
    subject_rows = _subject_rows(snapshots, classes)
    ranks = read_gradebook_rules(tenant, (CFG_RANK_IN_CLASS,)).rank_in_class
    return {
        "term": {"id": term.pk, "name": term.name, "session": term.session.name},
        "scope": {
            "branch_dimension": several,
            "classes": [
                {
                    "id": school_class.pk, "name": school_class.name,
                    "branch": {"id": home.pk, "name": home.name} if several else None,
                }
                for school_class, home in placed
            ],
            "published_count": len(placed),
        },
        "ranks": ranks,
        "subjects": subject_rows,
        "compare": _compare(subject_rows, class_order),
        "movers": _movers(tenant, term, snapshots),
        "tops": _tops(snapshots, classes) if ranks else [],
    }
