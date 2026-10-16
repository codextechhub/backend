"""Publishing a class's results: the snapshot, the release and the guardian's email.

**Publishing** takes a class that is awaiting the principal's approval and does,
in one transaction for that class, everything the school's families will rely on
afterwards:

1. every student on the roll gets a **snapshot**: the subject rows with their
   component cells (ABS and EXC kept as markers), totals, grades and remarks,
   the class figures, the overall average, the position where the school ranks,
   the term's attendance, the ratings where the school records them, both
   comments with their authors' names, the grading key and the day the next term
   begins. The last term of a school year with more than one term also carries
   the year's ``cumulative`` block (:mod:`.cumulative`). A later change to a
   scale, a sheet or a setting changes nothing that was published;
2. the class's stage becomes PUBLISHED, with who and when;
3. every student without an open fee hold is **released**; a held student is
   marked HELD and is not released, printed or emailed.

**The guardian's email** (``gradebook.report_card_ready``) goes to the primary
guardian of each released child, one per child, so a family with two children
at the school gets two. A guardian who has an account is written to through it
and so reads it in the app as well; one with only an address is an unregistered
recipient. It says only that the report card is ready to collect: the child's
first name, the term and year, the school and branch and the phone to call (the
class's branch phone, or the school's when the branch has none). A child whose
primary guardian has no email is recorded ``NO_EMAIL`` at once, and the rest are
written to once the transaction has committed, so a publication that rolled back
never emails anybody; each is then recorded ``SENT`` with the time. A failure to
send is logged and leaves the child released, without a state: publishing is the
act the school performed and the email is its consequence.

**Several classes** are published one at a time, each in its own transaction. A
class that is not awaiting approval, or whose sheets are not all in any more, is
refused with its reason and the others go ahead.

**Lifting a hold after publication** releases the student the same way
(:func:`release_after_lift`).

**Every snapshot is kept.** Publication writes each student's snapshot to
:class:`~..models.ResultVersion` as version 1 beside the copy on the result row,
and an approved amendment writes the next (:mod:`.amendments`). The pieces that
make one subject's row and a student's totals are functions of their own
(:func:`subject_row`, :func:`snapshot_totals`) so an amendment rebuilds a card
with the same arithmetic publication used.

**The updated-card email** (``gradebook.report_card_updated``) is written to the
same person, with the same context, as the ready email
(:func:`send_updated_notice`).
"""
from __future__ import annotations

import logging
from functools import partial

from django.db import transaction
from django.utils import timezone

from schools.vs_attendance.services.notices import primary_guardians
from schools.vs_attendance.services.term_summary import class_term_summary
from vs_audit.models import AuditActionType
from vs_notifications.notify import UnregisteredRecipient, send_notification
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import (
    EVENT_REPORT_CARD_READY,
    EVENT_REPORT_CARD_UPDATED,
    EmailState,
    StoredStage,
)
from ..exceptions import GradebookError, NotAwaiting, SubjectsMissing
from ..models import ClassTerm, PrincipalComment, ResultVersion, StudentTermResult
from . import approvals, audit, classbook, compute, cumulative, resolve, setup
from .classbook import ClassBook, ClassContext, percentage_of

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1


# ── What a family is told ──────────────────────────────────────────────────

def ready_context(tenant, branch, term, student) -> dict:
    """What ``gradebook.report_card_ready`` reads: nothing from the result.

    The branch is named only at a school with several, where it tells a parent
    which site the card waits at; at a school with one it repeats the school.
    ``tenant_phone`` is the class's branch phone, or the school's when the branch
    has none, and ``""`` when neither does (the template then leaves the line
    off). The email goes to an outside party, so the number is deliberately not
    narrowed by the school's Field Access switches on branch contact details.
    """
    several = only_branch_id_or_several(tenant) is None
    return {
        "student_first_name": student.first_name,
        "term_name": term.name,
        "session_name": term.session.name,
        "tenant_name": tenant.name,
        "branch_name": branch.name if several else "",
        "tenant_phone": resolve.contact_phone(tenant, branch),
    }


def decide_release(result, reach, now) -> None:
    """Release *result* now, recording the email outcome that is already known.

    A guardian with an address stays undecided until the email is sent; one
    without is ``NO_EMAIL`` now and nothing is sent to them.
    """
    result.released_at = now
    result.emailed_at = None
    result.email_state = None if reach is not None and reach.has_email else EmailState.NO_EMAIL


def _send_one(tenant, class_term, student, reach, event_key=EVENT_REPORT_CARD_READY) -> bool:
    if reach.user is not None:
        recipients, unregistered = [reach.user], None
    else:
        recipients = []
        unregistered = [UnregisteredRecipient(email=reach.email, name=reach.guardian.full_name)]
    sent = send_notification(
        event_key=event_key,
        context=ready_context(tenant, class_term.branch, class_term.term, student),
        recipients=recipients,
        unregistered_recipients=unregistered,
        tenant=tenant,
        branch=class_term.branch,
    )
    return bool(sent)


def send_ready_notices(tenant, class_term_id, student_ids) -> int:
    """Email the primary guardians of *student_ids*, one email per child; returns how many went.

    Called once the transaction that released the children has committed. A
    child is recorded ``SENT`` only when the notification layer carried it.
    """
    class_term = ClassTerm.objects.select_related(
        "branch", "term", "term__session",
    ).get(pk=class_term_id)
    results = {
        row.student_id: row
        for row in StudentTermResult.objects.filter(
            tenant=tenant, class_term=class_term, student_id__in=list(student_ids),
        ).select_related("student")
    }
    reach = primary_guardians(tenant, list(results))
    delivered = 0
    for student_id, result in results.items():
        found = reach.get(student_id)
        if found is None or not found.has_email:
            continue
        try:
            if _send_one(tenant, class_term, result.student, found):
                now = timezone.now()
                StudentTermResult.objects.filter(pk=result.pk).update(
                    email_state=EmailState.SENT, emailed_at=now, updated_at=now,
                )
                delivered += 1
        except Exception:
            logger.exception(
                "Report card notice for result %s could not be sent. The release stands.",
                result.pk,
            )
    return delivered


def send_after_commit(tenant, class_term_id, student_ids) -> None:
    """Queue the guardians' emails for when the surrounding transaction commits."""
    if student_ids:
        transaction.on_commit(
            partial(send_ready_notices, tenant, class_term_id, list(student_ids)),
        )


def send_updated_notice(tenant, class_term_id, student_id) -> bool:
    """Tell *student_id*'s primary guardian an updated report card is ready; True when it went.

    Called once the transaction that approved the correction has committed.
    Nothing is written to the result: its email state records the ready notice,
    and a guardian with no address is simply not written to. A failure to send
    is logged and leaves the correction approved.
    """
    class_term = ClassTerm.objects.select_related(
        "branch", "term", "term__session",
    ).get(pk=class_term_id)
    student = StudentTermResult.objects.select_related("student").get(
        tenant=tenant, class_term=class_term, student_id=student_id,
    ).student
    reach = primary_guardians(tenant, [student_id]).get(student_id)
    if reach is None or not reach.has_email:
        return False
    try:
        return _send_one(tenant, class_term, student, reach, EVENT_REPORT_CARD_UPDATED)
    except Exception:
        logger.exception(
            "Updated report card notice for student %s could not be sent. The correction stands.",
            student_id,
        )
        return False


def send_updated_after_commit(tenant, class_term_id, student_id) -> None:
    """Queue the updated-card email for when the surrounding transaction commits."""
    transaction.on_commit(partial(send_updated_notice, tenant, class_term_id, student_id))


# ── The snapshot ───────────────────────────────────────────────────────────

def _grading_key(bands) -> list:
    """The scale's bands, highest first, each with the mark it runs up to."""
    ordered = sorted(bands, key=lambda band: band.min_score, reverse=True)
    out, ceiling = [], 100
    for band in ordered:
        out.append({
            "min_score": band.min_score, "max_score": ceiling, "grade": band.grade,
            "remark": band.remark, "is_fail": band.is_fail,
        })
        ceiling = band.min_score - 1
    return out


def principal_comment_for(average, templates, overrides):
    """The principal's comment for *average*: the class's own for its band, else the template's.

    The band is the one with the highest lowest-average the printed (two-place)
    average reaches. None when the student has no average.
    """
    shown = compute.round2(average)
    if shown is None:
        return None
    for band_min, text in templates:
        if shown >= band_min:
            return overrides.get(band_min) or text
    return None


def _class_stats(column) -> compute.ClassStats:
    return compute.class_stats(
        line.result.total for line in column.lines.values()
        if line.result.percentage is not None
    )


def subject_row(subject, components, cells, result, stats) -> dict:
    """One subject's row of a snapshot: the cells, the worked-out line and the class figures.

    *subject* is ``{"id", "name"}``, *components* the structure's columns (each
    with ``pk``), *cells* the student's ``(mark, marker)`` pairs in that order,
    *result* their :class:`~.compute.RowResult` and *stats* the subject's
    :class:`~.compute.ClassStats`. Publication and an amendment both build a row
    here, so a corrected card is worked out exactly as the original was.
    """
    return {
        "subject": subject,
        "cells": [
            {"component": component.pk, "mark": mark, "marker": marker}
            for component, (mark, marker) in zip(components, cells)
        ],
        "total": result.total, "obtainable": result.obtainable,
        "percentage": percentage_of(result.percentage),
        "grade": result.grade, "remark": result.remark, "is_fail": result.is_fail,
        "class_average": str(stats.average) if stats.average is not None else None,
        "highest": stats.highest, "lowest": stats.lowest,
    }


def snapshot_totals(rows) -> tuple[int, int]:
    """``(total obtained, total obtainable)`` over the subject rows that have a total."""
    obtained = obtainable = 0
    for row in rows:
        if row["total"] is not None:
            obtained += row["total"]
            obtainable += row["obtainable"]
    return obtained, obtainable


def build_snapshots(bk: ClassBook, class_term, publisher, now) -> dict:
    """``{student_id: snapshot}`` for every student on the roll, as plain JSON values.

    Each subject row names its main teacher inside ``subject`` (an amendment
    copies that block, so a corrected card keeps the teacher the original
    named). Where the school shows class figures, ``class_average`` is the
    class's overall average beside the student's own; it is absent otherwise,
    and absent from cards published before it was recorded.
    """
    tenant, school_class, term = bk.tenant, bk.school_class, bk.term
    scale = resolve.scale_for(tenant, school_class)
    key = _grading_key(bk.bands)
    templates = [
        (row["band_min"], row["text"]) for row in setup.templates_payload(tenant)["templates"]
    ]
    overrides = {
        row.band_min: row.text
        for row in PrincipalComment.objects.filter(tenant=tenant, class_term=class_term)
    }
    counts = class_term_summary(tenant, school_class, term).students
    stats = {column.subject.pk: _class_stats(column) for column in bk.columns if column.is_in}
    next_start = classbook.next_term_start(tenant, term)
    class_teacher = school_class.class_teacher
    components = [
        {
            "id": c.pk, "name": c.name, "short_name": c.short_name,
            "max_score": c.max_score, "is_exam": c.is_exam,
        }
        for c in bk.components
    ]
    rules = bk.rules
    # The class's own overall average, frozen with each card beside the
    # student's, as the per-subject class averages are.
    published_averages = [a for a in bk.averages.values() if a is not None]
    class_average = (
        percentage_of(sum(published_averages) / len(published_averages))
        if published_averages else None
    )
    out = {}
    for student in bk.students:
        subjects = []
        for column in bk.columns:
            line = column.lines.get(student.pk)
            if line is None:
                continue
            teacher = column.item.teacher
            subjects.append(subject_row(
                {
                    "id": column.subject.pk, "name": column.subject.name,
                    "teacher": teacher.user.full_name if teacher is not None else None,
                },
                bk.components, line.cells, line.result, stats[column.subject.pk],
            ))
        obtained, obtainable = snapshot_totals(subjects)
        average = bk.averages.get(student.pk)
        mine = counts.get(student.pk)
        result_row = bk.results.get(student.pk)
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "published_at": now.isoformat(),
            "student": {
                "id": student.pk, "name": student.full_name,
                "admission_number": student.student_number,
            },
            "class": {"id": school_class.pk, "name": school_class.name},
            "class_size": len(bk.roll),
            "term": {
                "id": term.pk, "name": term.name,
                "start_date": term.start_date.isoformat(), "end_date": term.end_date.isoformat(),
            },
            "session": {"id": term.session_id, "name": term.session.name},
            "branch": {"id": bk.branch.pk, "name": bk.branch.name},
            "scale_id": scale.pk,
            "structure_id": class_term.structure_id,
            "components": components,
            "subjects": subjects,
            "total_obtained": obtained, "total_obtainable": obtainable,
            "overall_average": percentage_of(average),
            "attendance": {
                "opened": mine.opened if mine else 0,
                "present": mine.present if mine else 0,
                "absent": mine.absent if mine else 0,
            },
            "teacher_comment": result_row.teacher_comment if result_row else "",
            "class_teacher": class_teacher.user.full_name if class_teacher else None,
            "principal_comment": principal_comment_for(average, templates, overrides),
            "principal": publisher.full_name,
            "grading_key": key,
            "next_term_begins": next_start.isoformat() if next_start else None,
            "options": {
                "show_class_stats": rules.show_class_stats, "rating_key": rules.rating_key,
            },
        }
        if rules.show_class_stats:
            snapshot["class_average"] = class_average
        if rules.rank_in_class:
            snapshot["position"] = bk.positions.get(student.pk)
            snapshot["roll_size"] = len(bk.roll)
        if rules.conduct_ratings:
            snapshot["ratings"] = dict(result_row.ratings) if result_row else {}
            snapshot["traits"] = list(rules.rating_traits)
        out[student.pk] = snapshot
    terms = cumulative.session_terms(tenant, term)
    if cumulative.is_cumulative(terms, term):
        cumulative.attach(
            tenant, terms, term, out, ranks=rules.rank_in_class, roll_size=len(bk.roll),
        )
    return out


# ── Previewing and publishing ──────────────────────────────────────────────

def _awaiting_term(ctx: ClassContext, *, lock: bool = False) -> ClassTerm:
    query = ClassTerm.objects.filter(
        tenant=ctx.tenant, school_class=ctx.school_class, term=ctx.term,
    )
    class_term = (query.select_for_update() if lock else query).first()
    if class_term is None or class_term.stage != StoredStage.AWAITING:
        raise NotAwaiting()
    return class_term


def _held_ids(bk: ClassBook) -> tuple[list, set]:
    held = approvals.held_students(bk.tenant, bk.term, [bk.entry])[bk.school_class.pk]
    return held, {row["student"]["id"] for row in held}


def preview(ctx: ClassContext) -> dict:
    """What publishing the class now would do: how many are released, emailed or held."""
    _awaiting_term(ctx)
    bk = classbook.load_book(ctx.tenant, ctx.school_class, ctx.branch, ctx.term)
    held, held_ids = _held_ids(bk)
    releasing = [student.pk for student in bk.students if student.pk not in held_ids]
    reach = primary_guardians(ctx.tenant, releasing)
    emailed = sum(1 for pk in releasing if pk in reach and reach[pk].has_email)
    return {
        "class": {"id": ctx.school_class.pk, "name": ctx.school_class.name},
        "released": len(releasing), "emailed": emailed,
        "no_email": len(releasing) - emailed, "held": held,
    }


def publish_class(ctx: ClassContext) -> dict:
    """Publish the class in its own transaction; returns the counts, or raises its refusal."""
    now = timezone.now()
    with transaction.atomic():
        class_term = _awaiting_term(ctx, lock=True)
        bk = classbook.load_book(ctx.tenant, ctx.school_class, ctx.branch, ctx.term)
        missing = bk.missing
        if missing:
            raise SubjectsMissing(
                "A subject's sheet is no longer in, so the class cannot be published. "
                "Ask for the class to be returned.",
                subjects=[
                    {"id": column.subject.pk, "name": column.subject.name} for column in missing
                ],
            )
        snapshots = build_snapshots(bk, class_term, ctx.user, now)
        held, held_ids = _held_ids(bk)
        reach = primary_guardians(ctx.tenant, [student.pk for student in bk.students])

        created, changed, to_send = [], [], []
        for student in bk.students:
            result = bk.results.get(student.pk)
            if result is None:
                result = StudentTermResult(
                    tenant=ctx.tenant, class_term=class_term, student=student,
                )
                created.append(result)
            else:
                changed.append(result)
            result.snapshot = snapshots[student.pk]
            result.version = SNAPSHOT_VERSION
            if student.pk in held_ids:
                result.released_at, result.emailed_at = None, None
                result.email_state = EmailState.HELD
            else:
                decide_release(result, reach.get(student.pk), now)
                if result.email_state is None:
                    to_send.append(student.pk)
        if created:
            StudentTermResult.objects.bulk_create(created)
        if changed:
            StudentTermResult.objects.bulk_update(
                changed,
                ["snapshot", "version", "released_at", "emailed_at", "email_state"],
            )
        ResultVersion.objects.bulk_create([
            ResultVersion(
                tenant=ctx.tenant, result=result, version=SNAPSHOT_VERSION,
                snapshot=result.snapshot,
            )
            for result in created + changed
        ])
        class_term.stage = StoredStage.PUBLISHED
        class_term.published_by, class_term.published_at = ctx.user, now
        class_term.save(update_fields=["stage", "published_by", "published_at", "updated_at"])

        released = len(bk.students) - len(held_ids)
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="ClassTerm", entity_id=class_term.pk,
            entity_label=f"{ctx.school_class.name}, {ctx.term.name}",
            summary=(
                f"{ctx.user.full_name} published {ctx.school_class.name}, {ctx.term.name}: "
                f"{released} released, {len(held_ids)} held."
            ),
            class_id=ctx.school_class.pk, term_id=ctx.term.pk,
            released=released, held=len(held_ids), to_email=len(to_send),
            action_type=AuditActionType.UPDATE,
        )
        send_after_commit(ctx.tenant, class_term.pk, to_send)
    return {
        "released": released, "emailed": len(to_send),
        "no_email": released - len(to_send), "held": len(held_ids),
    }


def publish_classes(contexts) -> list:
    """Publish each class in *contexts* on its own; a refusal is that class's answer alone."""
    out = []
    for ctx in contexts:
        entry = {"class": {"id": ctx.school_class.pk, "name": ctx.school_class.name}}
        try:
            entry.update(published=True, **publish_class(ctx))
        except GradebookError as refusal:
            entry.update(
                published=False, code=refusal.error_code, message=refusal.message,
                released=0, emailed=0, no_email=0, held=0,
            )
        out.append(entry)
    return out


# ── Releasing one student after a hold is lifted ───────────────────────────

def release_after_lift(tenant, student, term, now) -> bool:
    """Release *student*'s published result for *term* if it is held; True when released.

    Called inside the lifting transaction. A result that was never published, or
    was already released, is left alone: lifting a hold before publication only
    lifts it. The guardian is written to once the transaction commits.
    """
    result = (
        StudentTermResult.objects.select_for_update()
        .filter(
            tenant=tenant, student=student, class_term__term=term,
            class_term__stage=StoredStage.PUBLISHED, snapshot__isnull=False,
            released_at__isnull=True,
        )
        .select_related("class_term")
        .order_by("-class_term__published_at").first()
    )
    if result is None:
        return False
    reach = primary_guardians(tenant, [student.pk]).get(student.pk)
    decide_release(result, reach, now)
    result.save(update_fields=["released_at", "emailed_at", "email_state", "updated_at"])
    if result.email_state is None:
        send_after_commit(tenant, result.class_term_id, [student.pk])
    return True
