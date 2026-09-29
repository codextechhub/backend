"""The end-of-session move.

**The preview and the run share this module's classification, and that is the
point.** A preview computed by different code from the run is not a preview, it
is a second opinion, and the two drift the first time either is fixed.

Four outcomes, not two categories. PROMOTE writes a placement at the next
level; REPEAT writes a placement in the *same* class for the next session,
which is a real write and not a no-op; GRADUATE ends the placement and leaves
the roll; HOLD leaves the student exactly where they are and writes nothing.

Nothing is ever silently skipped. A student the run will not touch appears on
the exception list with the reason, and the reasons are a fixed vocabulary
because the screen prints the sentence.

**The school's promotion rules** (``promotion_rules.py``) decide four things,
and each default is how every school promoted before it could choose:

* a suspended pupil is an exception under HOLD, and a candidate under
  PROMOTE, moved up with their year group and still suspended;
* a pupil who is confirmed but not placed (ENROLLED, holding a class in the
  year being left) defaults to HOLD, or to PROMOTE where the school says so.
  A run that places one of them, promoted or repeating, makes them ACTIVE, as
  giving them a class by hand does;
* SAME_ARM moves each arm up whole (JSS1 B to JSS2 B, or the first class at
  the level); SPREAD shares the pupils promoting into a level evenly across
  its classes (:func:`_spread`). A repeat keeps its arm either way;
* the capacity rule the run follows is the enrolment rule under
  FOLLOW_ENROLMENT, or the school's own WARN, HARD or OFF for the promotion.

Every target is a class the pupil may join: school-wide, or at their own
branch. The review screen's per-student overrides apply on top of all of it.

FRD M11 v2.4 FR-010.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_config.clock import tenant_today
from vs_audit.services import emit_audit_event

from ..constants import (
    ALLOWED_TRANSITIONS,
    CapacityMode,
    EXC_LEVEL_NOT_WIRED,
    EXC_NO_CLASS_AT_NEXT_LEVEL,
    EXC_NO_CLASS_ASSIGNED,
    EXC_NO_CLASS_TO_REPEAT,
    EXC_STUDENT_SUSPENDED,
    EXC_TERMINAL_LEVEL,
    EnrolmentOutcome,
    PromotionArms,
    PromotionNotPlaced,
    PromotionOutcome,
    PromotionSuspended,
    StudentStatus,
)
from ..models import ClassEnrolment, Student, StudentPromotionBatch
from .placement import write_enrolment
from .scoping import scope_classes, scope_students
from .years import assert_year_is_open

#: The sentences the exception list prints. Written for the person reading
#: them, because the screen shows them verbatim under the student's name.
EXCEPTION_TEXT = {
    EXC_TERMINAL_LEVEL: (
        "Terminal class, so these students graduate rather than moving up."
    ),
    EXC_NO_CLASS_AT_NEXT_LEVEL: (
        "There is no class at the next level yet, so these students cannot be "
        "promoted. Add one in Academic Structure first."
    ),
    EXC_LEVEL_NOT_WIRED: (
        "Nobody has set what this level promotes into, so these students are "
        "held rather than moved. Set its promotion target in Academic "
        "Structure, or mark the level as one pupils leave after."
    ),
    EXC_STUDENT_SUSPENDED: (
        "{name} is suspended, so they are not promoted with the cohort. Lift "
        "the suspension first, or move them by hand afterwards."
    ),
    EXC_NO_CLASS_ASSIGNED: (
        "{name} has no class, so there is nothing to promote from. Assign a "
        "class first."
    ),
    EXC_NO_CLASS_TO_REPEAT: (
        "This class does not exist in the new year, so these students cannot "
        "repeat it. Roll the year forward, or add the class in Academic "
        "Structure first."
    ),
}


@dataclass
class Candidate:
    student: Student
    enrolment: ClassEnrolment
    #: Where a PROMOTE lands, in the target year.
    target_class: object | None
    #: Where a REPEAT lands, in the target year. Not the class the student is
    #: in: that one belongs to the year being left, and a placement whose year
    #: and class disagree puts a child on a register nobody opens.
    repeat_class: object | None
    outcome: str


@dataclass
class Plan:
    candidates: list = field(default_factory=list)
    #: Class-wide causes, one entry per class however many students it covers.
    class_exceptions: list = field(default_factory=list)
    #: Per-student causes, one entry each.
    student_exceptions: list = field(default_factory=list)
    level_map: list = field(default_factory=list)
    #: Target classes the run would fill past their capacity, one entry each.
    over_capacity: list = field(default_factory=list)
    #: The EFFECTIVE capacity rule, WARN, HARD or OFF, which decides what the
    #: run does with them: the enrolment rule where the school's promotion
    #: rule follows it, the promotion rule otherwise.
    capacity_mode: str = CapacityMode.WARN.value
    #: The school's promotion rules the plan was made under
    #: (``promotion_rules.PromotionRules``).
    rules: object | None = None

    def counts(self):
        out = {v: 0 for v in PromotionOutcome.values}
        for c in self.candidates:
            out[c.outcome] += 1
        return out


def _target_class(source_class, target_classes_by_level_code, branch_id):
    """Where a source class promotes to, in the **target** session.

    Levels and classes belong to a year, and a year's structure is seeded from
    the previous one keeping each row's code. So the hop across sessions is by
    code, not by primary key: 2025/2026's JSS1 promotes to a level whose
    next_level is 2025/2026's JSS2, and the class the child actually joins is
    the 2026/2027 class at the level carrying JSS2's code.

    Getting this wrong is not a visible bug. Matching on next_level's own id
    would resolve to last year's JSS2 and place the whole cohort back into the
    year they just left, with every row looking perfectly valid.

    Same arm first - JSS1 B goes to JSS2 B - then any class at that level,
    because a school that renamed its arms should not have its cohort blocked.
    Only classes a pupil at *branch_id* may join are considered (see
    :func:`_class_at`).
    """
    level = source_class.level
    if level is None:
        return None, EXC_NO_CLASS_ASSIGNED
    # THREE states, not two. A null next_level means "leave the school" only
    # when the level says so; on its own it means nobody has wired the chain
    # yet, and the two must not be treated alike - Level.next_level's own
    # comment says so, and FRD v2.7 FR-005 requires the refusal.
    if getattr(level, "is_terminal", False):
        return None, EXC_TERMINAL_LEVEL
    if level.next_level_id is None:
        return None, EXC_LEVEL_NOT_WIRED

    found = _class_at(
        source_class, level.next_level, target_classes_by_level_code, branch_id,
    )
    return (found, None) if found else (None, EXC_NO_CLASS_AT_NEXT_LEVEL)


def _reachable(classes, branch_id):
    """The classes among *classes* a pupil at *branch_id* may join.

    A school-wide class or one at the pupil's own branch, the rule an ordinary
    placement keeps (``scoping.assert_class_reachable``). A whole-school run
    sees every branch's classes, and without this a Lekki pupil whose arm has
    no class at Lekki next year lands in Ikeja's.
    """
    return [c for c in classes if c.branch_id is None or c.branch_id == branch_id]


def _class_at(source_class, level, target_classes_by_level_code, branch_id):
    """The target year's class at *level* a pupil at *branch_id* may join, same arm first.

    Same arm because JSS1 B should become JSS2 B; any class at the level as a
    fallback, because a school that renamed its arms should not have its
    cohort blocked.
    """
    if level is None:
        return None
    candidates = _reachable(
        target_classes_by_level_code.get((level.code or "").lower(), []),
        branch_id,
    )
    if not candidates:
        return None
    arm = (getattr(source_class, "arm", "") or "").lower()
    same_arm = next(
        (c for c in candidates if (c.arm or "").lower() == arm), None,
    )
    return same_arm or candidates[0]


def _repeat_class(source_class, target_classes_by_level_code, branch_id):
    """Where a repeating student lands: the SAME level, in the target year.

    Not the class they are already in. That row belongs to the year being
    left, and writing it against the new year produces an enrolment whose two
    halves name different years - which every screen renders as normal,
    because both years call the class JSS1 A, and which the new year's
    register simply does not include.
    """
    return _class_at(
        source_class, source_class.level, target_classes_by_level_code,
        branch_id,
    )


def classify(tenant, user, *, from_session, to_session, overrides=None,
             branch=None):
    """Work out what would happen. Writes nothing.

    ``overrides`` is ``{student_id: outcome}`` from the review screen, applied
    on top of the defaults.

    *branch* narrows the run to one site: its students, and its classes plus
    the school-wide ones as targets. It is read by the preview AND the run
    through this one function, which is the point - a preview computed by
    different code from the run is not a preview, it is a second opinion.

    The school's promotion rules (``promotion_rules.py``) are read here once:
    whether a suspended pupil is an exception or a candidate, whether a
    confirmed but unplaced pupil is held or promoted by default, whether the
    promoted pupils keep their arm or are spread (:func:`_spread`), and which
    capacity rule the run follows.
    """
    from django.db.models import Q as _Q
    from schools.vs_academics.models import SchoolClass

    from .promotion_rules import read_promotion_rules

    # Here rather than in run(), so the preview refuses exactly what the run
    # would - and before run()'s per-student except, which would otherwise
    # swallow this into a "failed" tally instead of answering the caller.
    # from_session is deliberately not checked: promoting OUT of last year is
    # the whole point of the run.
    assert_year_is_open(to_session, what="promote")

    overrides = overrides or {}
    rules = read_promotion_rules(tenant)
    plan = Plan(rules=rules, capacity_mode=rules.effective_capacity_mode)

    # Only the TARGET year's classes are promotion targets. Keyed by their
    # level's code, which is the identifier that survives the roll-forward.
    target_class_rows = scope_classes(
        SchoolClass.objects.filter(
            tenant=tenant, is_active=True, session=to_session,
        ),
        user, tenant,
    )
    if branch is not None:
        # That site's classes plus the school-wide ones, which is what a null
        # branch means. A class at another site is not somewhere these students
        # can go.
        target_class_rows = target_class_rows.filter(
            _Q(branch=branch) | _Q(branch__isnull=True),
        )
    target_classes = target_class_rows.select_related("level", "branch")
    by_level_code: dict[str, list] = {}
    for row in target_classes:
        if row.level_id and row.level.code:
            by_level_code.setdefault(row.level.code.lower(), []).append(row)

    on_roll_rows = scope_students(
        Student.objects.filter(
            tenant=tenant,
            status__in=[StudentStatus.ACTIVE, StudentStatus.ENROLLED,
                        StudentStatus.SUSPENDED],
        ),
        user, tenant,
    )
    if branch is not None:
        on_roll_rows = on_roll_rows.filter(branch=branch)
    on_roll = on_roll_rows.select_related("branch")

    enrolments = {
        e.student_id: e
        for e in ClassEnrolment.objects.filter(
            tenant=tenant, session=from_session, is_active=True,
        ).select_related(
        "school_class", "school_class__level", "school_class__level__next_level",
        "school_class__branch",
    )
    }

    seen_class_cause: set[tuple[int, str]] = set()
    per_class_counts: dict[int, int] = {}
    # Pupils each class-wide cause covers: a school-wide class holds pupils of
    # several branches, and a cause can reach some of them and not others.
    per_cause_counts: dict[tuple[int, str], int] = {}

    for student in on_roll:
        enrolment = enrolments.get(student.pk)

        # Derived from the roll, not from the candidate set: a student excluded
        # from candidacy is exactly the one who most needs naming here.
        if enrolment is None:
            plan.student_exceptions.append({
                "student": student.pk, "name": student.full_name,
                "class": None, "cause": EXC_NO_CLASS_ASSIGNED,
                "reason": EXCEPTION_TEXT[EXC_NO_CLASS_ASSIGNED].format(
                    name=student.first_name,
                ),
            })
            continue
        if (
            student.status == StudentStatus.SUSPENDED
            and rules.suspended == PromotionSuspended.HOLD
        ):
            plan.student_exceptions.append({
                "student": student.pk, "name": student.full_name,
                "class": enrolment.school_class.name,
                "cause": EXC_STUDENT_SUSPENDED,
                "reason": EXCEPTION_TEXT[EXC_STUDENT_SUSPENDED].format(
                    name=student.first_name,
                ),
            })
            continue

        source = enrolment.school_class
        per_class_counts[source.pk] = per_class_counts.get(source.pk, 0) + 1
        target, cause = _target_class(source, by_level_code, student.branch_id)
        repeat_target = _repeat_class(source, by_level_code, student.branch_id)
        can_graduate = StudentStatus.GRADUATED in ALLOWED_TRANSITIONS.get(
            student.status, frozenset(),
        )

        if cause is not None:
            key = (source.pk, cause)
            per_cause_counts[key] = per_cause_counts.get(key, 0) + 1
            if key not in seen_class_cause:
                seen_class_cause.add(key)
                plan.class_exceptions.append({
                    "class": source.pk, "class_name": source.name,
                    "cause": cause, "reason": EXCEPTION_TEXT[cause],
                    "students": 0,
                })

        if cause == EXC_TERMINAL_LEVEL and not can_graduate:
            # Only an active pupil can graduate; the transition table refuses
            # anyone else, and the run would count them as failed.
            default = PromotionOutcome.HOLD
        elif cause == EXC_TERMINAL_LEVEL:
            default = PromotionOutcome.GRADUATE
        elif cause in (EXC_NO_CLASS_AT_NEXT_LEVEL, EXC_LEVEL_NOT_WIRED):
            # Held, never graduated. An unwired level is a gap in the school's
            # setup, and the safe reading of a gap is "do nothing", not "these
            # children have finished school".
            default = PromotionOutcome.HOLD
        elif (
            student.status == StudentStatus.ENROLLED
            and rules.not_placed == PromotionNotPlaced.HOLD
        ):
            # Confirmed but never placed into attendance. Moving them up a
            # level they have not sat is a decision a person takes, unless
            # the school has said to move them with their class.
            default = PromotionOutcome.HOLD
        else:
            default = PromotionOutcome.PROMOTE

        outcome = overrides.get(str(student.pk), overrides.get(student.pk, default))
        if outcome not in PromotionOutcome.values:
            outcome = default
        if outcome == PromotionOutcome.GRADUATE and not can_graduate:
            outcome = PromotionOutcome.HOLD

        # A repeat with nowhere to land is named too, and only when it is the
        # chosen outcome: every class is missing from a year nobody has rolled
        # forward, and saying so about classes nobody is repeating is noise.
        if outcome == PromotionOutcome.REPEAT and repeat_target is None:
            key = (source.pk, EXC_NO_CLASS_TO_REPEAT)
            per_cause_counts[key] = per_cause_counts.get(key, 0) + 1
            if key not in seen_class_cause:
                seen_class_cause.add(key)
                plan.class_exceptions.append({
                    "class": source.pk, "class_name": source.name,
                    "cause": EXC_NO_CLASS_TO_REPEAT,
                    "reason": EXCEPTION_TEXT[EXC_NO_CLASS_TO_REPEAT],
                    "students": 0,
                })

        plan.candidates.append(
            Candidate(student, enrolment, target, repeat_target, outcome),
        )

    for entry in plan.class_exceptions:
        entry["students"] = per_cause_counts.get((entry["class"], entry["cause"]), 0)

    if rules.arms == PromotionArms.SPREAD:
        _spread(plan.candidates, by_level_code, to_session)

    plan.level_map = _level_map(plan.candidates, per_class_counts)
    # A school that does not check capacity has no class to name.
    plan.over_capacity = (
        [] if plan.capacity_mode == CapacityMode.OFF
        else _over_capacity(plan.candidates, to_session)
    )
    return plan


def _spread(candidates, target_classes_by_level_code, to_session):
    """Share the pupils promoting into each level evenly across its classes.

    Rewrites ``target_class`` on every PROMOTE candidate that has one. Each
    pupil, taken in order of last name, first name and id, joins the class
    at their next level with the fewest pupils, ties broken by class name:
    the load of a class is the seats already taken in the target year, plus
    the pupils repeating into it, plus the pupils this allocation has already
    given it. A pupil only ever joins a class they may join
    (:func:`_reachable`), so a branch run spreads over that branch's classes
    and the school-wide ones, and a whole-school run keeps each pupil at
    their own branch.

    A pupil already placed in the target year is skipped by the run, so they
    are not allocated again: their seat is counted where it is, and their
    target is that class. The order is fixed by the pupils and classes alone,
    never by the database, so the preview and the run allocate identically.
    Two queries whatever the size of the cohort.
    """
    from django.db.models import Count

    pool = {
        c.pk: c for rows in target_classes_by_level_code.values() for c in rows
    }
    moving = [
        c for c in candidates
        if c.outcome in (PromotionOutcome.PROMOTE, PromotionOutcome.REPEAT)
    ]
    if not moving or not pool:
        return
    placed = dict(
        ClassEnrolment.objects.filter(
            session=to_session, is_active=True,
            student_id__in=[c.student.pk for c in moving],
        ).values_list("student_id", "school_class_id"),
    )
    load = dict(
        ClassEnrolment.objects.filter(
            session=to_session, is_active=True, school_class_id__in=list(pool),
        ).values("school_class_id").annotate(n=Count("id")).values_list(
            "school_class_id", "n",
        ),
    )
    for cand in moving:
        if (
            cand.outcome == PromotionOutcome.REPEAT
            and cand.repeat_class is not None
            and cand.student.pk not in placed
        ):
            load[cand.repeat_class.pk] = load.get(cand.repeat_class.pk, 0) + 1

    promoting = sorted(
        (
            c for c in moving
            if c.outcome == PromotionOutcome.PROMOTE and c.target_class is not None
        ),
        key=lambda c: (
            (c.student.last_name or "").casefold(),
            (c.student.first_name or "").casefold(),
            c.student.pk,
        ),
    )
    for cand in promoting:
        already = placed.get(cand.student.pk)
        if already is not None:
            if already in pool:
                cand.target_class = pool[already]
            continue
        code = (cand.target_class.level.code or "").lower()
        classes = _reachable(
            target_classes_by_level_code.get(code, []), cand.student.branch_id,
        )
        if not classes:
            continue
        chosen = min(
            classes, key=lambda c: (load.get(c.pk, 0), c.name.casefold(), c.pk),
        )
        cand.target_class = chosen
        load[chosen.pk] = load.get(chosen.pk, 0) + 1


def _level_map(candidates, per_class_counts):
    """One row per source class: where its pupils go, and how many to each class.

    ``to_classes`` lists the target classes receiving the class's PROMOTE
    pupils, by name, each with its count. Keeping arms gives one entry, or
    one per branch where a school-wide class holds pupils of several;
    spreading gives several. ``to`` names them all, joined with ", ", and
    ``to_id`` is set only when there is exactly one. A class none of whose
    pupils is promoting has an empty list, and ``to`` and ``to_id`` name
    where the class would go, so the map still shows the route.
    """
    rows: dict = {}
    for cand in candidates:
        source = cand.enrolment.school_class
        row = rows.get(source.pk)
        if row is None:
            # Only a level that SAYS pupils leave is terminal here. An unwired
            # one renders as "not set" rather than "Graduates", so the map
            # cannot tell a registrar a cohort is leaving when nobody has
            # said that.
            terminal = source.level is not None and getattr(
                source.level, "is_terminal", False,
            )
            row = rows[source.pk] = {
                "from": source.name, "from_id": source.pk,
                "to": cand.target_class.name if cand.target_class else None,
                "to_id": cand.target_class.pk if cand.target_class else None,
                "terminal": bool(terminal),
                "students": per_class_counts.get(source.pk, 0),
                "to_classes": {},
            }
        if cand.outcome == PromotionOutcome.PROMOTE and cand.target_class is not None:
            target = cand.target_class
            entry = row["to_classes"].setdefault(
                target.pk, {"id": target.pk, "name": target.name, "students": 0},
            )
            entry["students"] += 1

    out = []
    for row in rows.values():
        to_classes = sorted(
            row["to_classes"].values(),
            key=lambda e: (e["name"].casefold(), e["id"]),
        )
        row["to_classes"] = to_classes
        if len(to_classes) == 1:
            row["to"], row["to_id"] = to_classes[0]["name"], to_classes[0]["id"]
        elif len(to_classes) > 1:
            row["to"] = ", ".join(e["name"] for e in to_classes)
            row["to_id"] = None
        out.append(row)
    out.sort(key=lambda r: r["from"])
    return out


def _over_capacity(candidates, to_session):
    """The target classes this run would fill past their capacity.

    The same arithmetic as placing one child (placement.capacity_state): seats
    already taken in the target year plus the pupils this run adds, against a
    capacity where one is set. A pupil already placed in the target year is
    skipped by the run, so they are counted once, as a seat taken, and never
    again as an arrival. Two queries whatever the size of the cohort.
    """
    from django.db.models import Count

    placed = set(
        ClassEnrolment.objects.filter(
            session=to_session, is_active=True,
            student_id__in=[c.student.pk for c in candidates],
        ).values_list("student_id", flat=True),
    )
    arriving: dict = {}
    classes: dict = {}
    for cand in candidates:
        if cand.student.pk in placed:
            continue
        target = (
            cand.repeat_class if cand.outcome == PromotionOutcome.REPEAT
            else cand.target_class if cand.outcome == PromotionOutcome.PROMOTE
            else None
        )
        if target is None or target.capacity is None:
            continue
        arriving[target.pk] = arriving.get(target.pk, 0) + 1
        classes[target.pk] = target
    if not arriving:
        return []
    taken = dict(
        ClassEnrolment.objects.filter(
            school_class_id__in=list(arriving), session=to_session, is_active=True,
        ).values("school_class_id").annotate(n=Count("id")).values_list(
            "school_class_id", "n",
        ),
    )
    out = []
    for pk, adding in arriving.items():
        school_class = classes[pk]
        used = taken.get(pk, 0)
        if used + adding > school_class.capacity:
            out.append({
                "class": pk, "class_name": school_class.name,
                "capacity": school_class.capacity, "used": used,
                "adding": adding, "over_by": used + adding - school_class.capacity,
            })
    out.sort(key=lambda r: r["class_name"])
    return out


@transaction.atomic
def _apply_one(cand, *, to_session, actor):
    """One student's promotion, in its own transaction and idempotent.

    Re-running skips a student already placed in the target session, which is
    what makes a batch restartable after a partial failure without placing
    anybody twice.

    Statuses change in two cases only. A graduating pupil becomes GRADUATED.
    A pupil who is confirmed but not placed (ENROLLED) and is given a class,
    promoted or repeating, becomes ACTIVE, exactly as a placement by hand
    makes them (``placement.place``); leaving them ENROLLED would put a child
    with a class on no register that lists active pupils. A suspended pupil
    stays SUSPENDED: the suspension is theirs, not the promotion's.
    """
    from .status import transition

    student = cand.student
    if ClassEnrolment.objects.filter(
        student=student, session=to_session, is_active=True,
    ).exists():
        return None

    if cand.outcome == PromotionOutcome.GRADUATE:
        transition(
            student, StudentStatus.GRADUATED, actor=actor, system=True,
            reason=f"Graduated at the end of {cand.enrolment.session}.",
        )
        return PromotionOutcome.GRADUATE

    if cand.outcome == PromotionOutcome.HOLD:
        return PromotionOutcome.HOLD

    target = (
        cand.repeat_class if cand.outcome == PromotionOutcome.REPEAT
        else cand.target_class
    )
    if target is None:
        # Asked to promote with nowhere to go. Held rather than failed: the
        # student is unchanged and the count says so.
        return PromotionOutcome.HOLD

    cand.enrolment.is_active = False
    cand.enrolment.ended_at = timezone.now()
    cand.enrolment.outcome = (
        EnrolmentOutcome.REPEATED if cand.outcome == PromotionOutcome.REPEAT
        else EnrolmentOutcome.PROMOTED
    )
    cand.enrolment.save(
        update_fields=["is_active", "ended_at", "outcome", "updated_at"],
    )
    # Through the same writer as an ordinary placement, so the year on the row
    # is derived from the class here too. to_session is what the run meant to
    # write into and is checked against the class, never stored from.
    write_enrolment(
        student=student, school_class=target, intended_year=to_session,
        actor=actor, is_active=True,
        effective_date=tenant_today(student.tenant),
        outcome=EnrolmentOutcome.CURRENT, assigned_by=actor,
    )
    if student.status == StudentStatus.ENROLLED:
        transition(
            student, StudentStatus.ACTIVE, actor=actor, system=True,
            reason=f"Placed in {target.name} by the end-of-year promotion.",
        )
    return cand.outcome


def run(tenant, user, *, from_session, to_session, overrides=None, branch=None,
        allow_over_capacity=False):
    """Run the promotion and record what happened.

    Each student is its own transaction, so one failure does not undo the
    students already moved - which is what makes the batch restartable.

    A run that would fill classes past their capacity follows the school's
    promotion capacity rule, which is the enrolment rule unless the school
    has set one of its own (``Plan.capacity_mode``). Under WARN it is refused
    with ``PROMOTION_OVER_CAPACITY`` until the caller acknowledges it
    (``allow_over_capacity``); under HARD it is refused with ``CLASS_FULL``
    whatever the caller sends, before anybody is moved; under OFF nothing is
    counted. The preview lists those classes first.
    """
    # The branch travels INTO the classification, not just onto the batch row.
    # Stamped on the record alone, a run labelled "Main Branch" promotes every
    # branch's students and the label is the only thing that says otherwise.
    plan = classify(
        tenant, user, from_session=from_session, to_session=to_session,
        overrides=overrides, branch=branch,
    )
    if plan.over_capacity and plan.capacity_mode == CapacityMode.HARD:
        from ..exceptions import ClassFull

        names = ", ".join(
            f"{row['class_name']} ({row['used'] + row['adding']} of {row['capacity']})"
            for row in plan.over_capacity
        )
        raise ClassFull(
            f"This promotion would put {names} over capacity, and this school "
            f"does not put classes over capacity. Add a class or move students "
            f"first.",
            classes=plan.over_capacity,
        )
    if plan.over_capacity and not allow_over_capacity:
        from ..exceptions import PromotionOverCapacity

        names = ", ".join(
            f"{row['class_name']} ({row['used'] + row['adding']} of {row['capacity']})"
            for row in plan.over_capacity
        )
        raise PromotionOverCapacity(
            f"This promotion would put {names} over capacity. You can go ahead "
            f"anyway.",
            classes=plan.over_capacity,
        )
    batch = StudentPromotionBatch.objects.create(
        tenant=tenant, branch=branch, from_session=from_session,
        to_session=to_session, initiated_by=user,
        total=len(plan.candidates),
        excluded=len(plan.student_exceptions),
    )
    tally = {"promoted": 0, "repeated": 0, "graduated": 0, "held": 0, "failed": 0}
    for cand in plan.candidates:
        try:
            result = _apply_one(cand, to_session=to_session, actor=user)
        except Exception:  # noqa: BLE001 - one student must not stop the batch
            tally["failed"] += 1
            continue
        if result == PromotionOutcome.PROMOTE:
            tally["promoted"] += 1
        elif result == PromotionOutcome.REPEAT:
            tally["repeated"] += 1
        elif result == PromotionOutcome.GRADUATE:
            tally["graduated"] += 1
        elif result == PromotionOutcome.HOLD:
            tally["held"] += 1
        # None means already placed in the target session: idempotent re-run,
        # counted as nothing rather than as a failure.

    for key, value in tally.items():
        setattr(batch, key, value)
    batch.save(update_fields=[*tally.keys(), "updated_at"])

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_PROMOTION_RUN,
        entity_type="StudentPromotionBatch", entity_id=str(batch.pk),
        entity_label=f"{from_session} to {to_session}",
        tenant=tenant, actor_user=user,
        summary=(
            f"Promotion run from {from_session} to {to_session}: "
            f"{tally['promoted']} promoted, {tally['repeated']} repeating, "
            f"{tally['graduated']} graduated, {tally['held']} held."
        ),
        metadata={**tally, "total": batch.total, "excluded": batch.excluded},
    )
    return batch, plan
