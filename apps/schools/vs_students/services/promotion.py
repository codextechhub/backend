"""The end-of-session move.

**The preview and the run share this module's classification, and that is the
point.** A preview computed by different code from the run is not a preview, it
is a second opinion, and the two drift the first time either is fixed.

Four outcomes, not two categories. PROMOTE writes a placement at the next
level; REPEAT writes a placement in the *same* class for the next session,
which is a real write and not a no-op; GRADUATE ends the placement and leaves
the roll; HOLD leaves the student exactly where they are and writes nothing.

Nothing is ever silently skipped. A student the run will not touch appears on
the exception list with the reason, and the four reasons are a fixed vocabulary
because the screen prints the sentence.

FRD M11 v2.4 FR-010.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    EXC_NO_CLASS_AT_NEXT_LEVEL,
    EXC_NO_CLASS_ASSIGNED,
    EXC_NO_CLASS_TO_REPEAT,
    EXC_STUDENT_SUSPENDED,
    EXC_TERMINAL_LEVEL,
    EnrolmentOutcome,
    PromotionOutcome,
    StudentStatus,
)
from ..models import ClassEnrolment, Student, StudentPromotionBatch
from .placement import assert_class_is_in_session
from .scoping import scope_classes, scope_students

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

    def counts(self):
        out = {v: 0 for v in PromotionOutcome.values}
        for c in self.candidates:
            out[c.outcome] += 1
        return out


def _target_class(source_class, target_classes_by_level_code):
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
    """
    level = source_class.level
    if level is None:
        return None, EXC_NO_CLASS_ASSIGNED
    if getattr(level, "is_terminal", False) or level.next_level_id is None:
        return None, EXC_TERMINAL_LEVEL

    found = _class_at(source_class, level.next_level, target_classes_by_level_code)
    return (found, None) if found else (None, EXC_NO_CLASS_AT_NEXT_LEVEL)


def _class_at(source_class, level, target_classes_by_level_code):
    """The target year's class at *level*, same arm first.

    Same arm because JSS1 B should become JSS2 B; any class at the level as a
    fallback, because a school that renamed its arms should not have its
    cohort blocked.
    """
    if level is None:
        return None
    candidates = target_classes_by_level_code.get((level.code or "").lower(), [])
    if not candidates:
        return None
    arm = (getattr(source_class, "arm", "") or "").lower()
    same_arm = next(
        (c for c in candidates if (c.arm or "").lower() == arm), None,
    )
    return same_arm or candidates[0]


def _repeat_class(source_class, target_classes_by_level_code):
    """Where a repeating student lands: the SAME level, in the target year.

    Not the class they are already in. That row belongs to the year being
    left, and writing it against the new year produces an enrolment whose two
    halves name different years - which every screen renders as normal,
    because both years call the class JSS1 A, and which the new year's
    register simply does not include.
    """
    return _class_at(
        source_class, source_class.level, target_classes_by_level_code,
    )


def classify(tenant, user, *, from_session, to_session, overrides=None):
    """Work out what would happen. Writes nothing.

    ``overrides`` is ``{student_id: outcome}`` from the review screen, applied
    on top of the defaults.
    """
    from schools.vs_academics.models import SchoolClass

    overrides = overrides or {}
    plan = Plan()

    # Only the TARGET year's classes are promotion targets. Keyed by their
    # level's code, which is the identifier that survives the roll-forward.
    target_classes = scope_classes(
        SchoolClass.objects.filter(
            tenant=tenant, is_active=True, session=to_session,
        ),
        user, tenant,
    ).select_related("level", "branch")
    by_level_code: dict[str, list] = {}
    for row in target_classes:
        if row.level_id and row.level.code:
            by_level_code.setdefault(row.level.code.lower(), []).append(row)

    on_roll = scope_students(
        Student.objects.filter(
            tenant=tenant,
            status__in=[StudentStatus.ACTIVE, StudentStatus.ENROLLED,
                        StudentStatus.SUSPENDED],
        ),
        user, tenant,
    ).select_related("branch")

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
        if student.status == StudentStatus.SUSPENDED:
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
        target, cause = _target_class(source, by_level_code)
        repeat_target = _repeat_class(source, by_level_code)

        if cause is not None and (source.pk, cause) not in seen_class_cause:
            seen_class_cause.add((source.pk, cause))
            plan.class_exceptions.append({
                "class": source.pk, "class_name": source.name,
                "cause": cause, "reason": EXCEPTION_TEXT[cause],
                "students": 0,
            })

        if cause == EXC_TERMINAL_LEVEL:
            default = PromotionOutcome.GRADUATE
        elif cause == EXC_NO_CLASS_AT_NEXT_LEVEL:
            default = PromotionOutcome.HOLD
        elif student.status == StudentStatus.ENROLLED:
            # Confirmed but never placed into attendance. Moving them up a
            # level they have not sat is a decision a person takes.
            default = PromotionOutcome.HOLD
        else:
            default = PromotionOutcome.PROMOTE

        outcome = overrides.get(str(student.pk), overrides.get(student.pk, default))
        if outcome not in PromotionOutcome.values:
            outcome = default

        # A repeat with nowhere to land is named too, and only when it is the
        # chosen outcome: every class is missing from a year nobody has rolled
        # forward, and saying so about classes nobody is repeating is noise.
        if (
            outcome == PromotionOutcome.REPEAT
            and repeat_target is None
            and (source.pk, EXC_NO_CLASS_TO_REPEAT) not in seen_class_cause
        ):
            seen_class_cause.add((source.pk, EXC_NO_CLASS_TO_REPEAT))
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
        entry["students"] = per_class_counts.get(entry["class"], 0)

    seen_map = set()
    for cand in plan.candidates:
        source = cand.enrolment.school_class
        if source.pk in seen_map:
            continue
        seen_map.add(source.pk)
        terminal = (
            source.level is None
            or getattr(source.level, "is_terminal", False)
            or source.level.next_level_id is None
        )
        plan.level_map.append({
            "from": source.name, "from_id": source.pk,
            "to": cand.target_class.name if cand.target_class else None,
            "to_id": cand.target_class.pk if cand.target_class else None,
            "terminal": bool(terminal),
            "students": per_class_counts.get(source.pk, 0),
        })
    plan.level_map.sort(key=lambda r: r["from"])
    return plan


@transaction.atomic
def _apply_one(cand, *, to_session, actor):
    """One student's promotion, in its own transaction and idempotent.

    Re-running skips a student already placed in the target session, which is
    what makes a batch restartable after a partial failure without placing
    anybody twice.
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
    # The same rule the ordinary placement obeys. _target_class picks from the
    # target year by design, so this should never fire - which is exactly when
    # a guard is worth having, because nothing else would notice if it changed.
    assert_class_is_in_session(target, to_session)
    ClassEnrolment.objects.create(
        tenant=student.tenant, student=student, school_class=target,
        session=to_session, is_active=True,
        effective_date=timezone.localdate(),
        outcome=EnrolmentOutcome.CURRENT, assigned_by=actor,
    )
    return cand.outcome


def run(tenant, user, *, from_session, to_session, overrides=None, branch=None):
    """Run the promotion and record what happened.

    Each student is its own transaction, so one failure does not undo the
    students already moved - which is what makes the batch restartable.
    """
    plan = classify(
        tenant, user, from_session=from_session, to_session=to_session,
        overrides=overrides,
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
