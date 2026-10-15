"""Amendments: correcting one subject of a result the school has already published.

A published result changes only this way. Someone with ``amendment.create``
names a student, a subject, the corrected marks and a reason; the principal
(``amendment.approve``), who may not be the person who asked, approves or
rejects it. Nothing about the published card moves until it is approved.

**Raising** needs a class-term that is PUBLISHED (``NOT_PUBLISHED``), a student
the class published a result for, a subject on that student's card
(``SUBJECT_NOT_TAKEN``) and a reason (``REASON_REQUIRED``). The cells are judged
exactly as a sheet's save judges them (:func:`.sheets.check_batch`: whole marks,
within the column's maximum, ABS or EXC), against the columns the result was
published with, and a published result has a value in every column so a blank is
refused. ``cells`` may name only the columns being changed. The amendment keeps
the subject's cells as published (``before``) and as the correction leaves them
(``after``). A correction identical to what was published is refused
(``NOTHING_CHANGED``) and only one waits at a time for a student's subject in a
term (``AMENDMENT_PENDING``), which the database also holds.

**Approving**, in one transaction that first locks the student's result row, so
a second approval of the same amendment finds it decided (``NOT_PENDING``):

1. the sheet's score rows take the corrected cells and record who asked for the
   correction and when it was approved (the sheet stays locked);
2. the student's snapshot is rebuilt as the next version
   (:func:`corrected_snapshot`) and the version it replaces stays in
   :class:`~..models.ResultVersion`, which holds every snapshot ever issued;
3. the amendment is closed with who decided it, when and the note.

Only the amended student's card changes. The amended subject's row, the
student's totals and their overall average are worked out again from the cells
and the grading key the card was published with (a scale edited since changes
nothing). The position is where the new average falls among the averages the
classmates were published with, so it can tie one of them without moving them.
The amended subject's class average, highest and lowest are worked out again for
this card from the classmates' published totals and the new total, so the card
never shows a total above its own highest. A cumulative term's card also has
its year block refreshed (:func:`.cumulative.refreshed`): the amended term's
figures, the subject and overall annual averages, and the annual position
against the classmates' published annual averages. No classmate's snapshot is
touched. Every key the rebuild does not recompute (the attendance, the comments, the
ratings, the grading key, the other subjects' class figures) is carried over
unchanged.

**Later cards of the same school year.** A later published term of the same
session whose card for this student carries a cumulative block shows the amended
term's figures in that block, so the correction reaches it too
(:func:`reissue_later_cards`). Each such card is reissued as its next version for
this student only: the block is rebuilt by the same :func:`.cumulative.refreshed`
the amended card uses, against the classmates' published annual averages of that
later class-term, and the version it replaces is kept. Every other key of that
card, and every classmate's card, is carried over untouched. The later card is
linked to the same amendment in :class:`~..models.ResultVersion`, which holds at
most one version of a result for an amendment. A later card whose block comes out
identical (the correction moved neither a total nor a percentage) is not
reissued. The guardian is written to once for each reissued card that was
released, so a released first term and third term send two emails. The rows of the
amended result and of every later card are locked together in result-id order, so
two corrections for the same student cannot wait on each other.

The principal's comment is the exception that follows its trigger: it is picked
by the band the overall average falls in, so when the correction moves the
average into another band the comment for that band is used (the class's own
for it if the principal wrote one, else the school's template), and otherwise
the published comment stands.

**Telling the family.** When the result was released, the primary guardian is
sent ``gradebook.report_card_updated`` once the transaction has committed, in
the words and to the person ``report_card_ready`` uses
(:func:`.publishing.send_updated_notice`). A held result, which has not been
released, gets nothing: the guardian is written to when the hold is lifted, and
reads the corrected card.

**Rejecting** needs a note (``NOTE_REQUIRED``) and changes nothing else.

**Branch.** An amendment belongs to its class-term's branch. A caller reaches
the amendments of the branches they can see, and one outside them is a 404.

The audit trail records that an amendment was raised and how it was decided by
whom. It carries ids only: the reason, the note and the marks are fields a role
may be denied, and the trail has no switch for them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Prefetch, Q, When
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from schools.vs_academics.models import Subject
from schools.vs_students.models import Student
from vs_audit.models import AuditActionType
from vs_rbac.scoping import only_branch_id_or_several, transaction_branch_q_for_user

from ..constants import AmendmentStatus, HistoryKind, StoredStage
from ..exceptions import (
    AmendmentPending,
    AmendmentReasonRequired,
    NoteRequired,
    NothingChanged,
    NotPending,
    NotPublished,
    SelfDecision,
    SubjectNotTaken,
)
from ..models import Amendment, ResultVersion, Score, ScoreSheet, StudentTermResult
from . import access, audit, compute, cumulative, publishing, resolve, setup, sheets
from .classbook import percentage_of

NOT_PUBLISHED_FOR_AMENDMENT = (
    "Only a published result can be corrected. This class's results have not "
    "been published for this term yet, so change the marks on the score sheet."
)
NO_SUCH_RESULT = "No such student on this class's published results."
NO_SUCH_AMENDMENT = "No such amendment at this school."


# ── The cells and the arithmetic (no database) ─────────────────────────────

def _components(snapshot) -> list:
    """The columns the result was published with, each with ``pk`` and ``max_score``."""
    return [
        SimpleNamespace(
            pk=c["id"], max_score=c["max_score"], name=c["name"],
            short_name=c["short_name"], is_exam=c["is_exam"],
        )
        for c in snapshot["components"]
    ]


def _bands(snapshot) -> list:
    """The grading key the result was published with, as bands the arithmetic reads."""
    return [
        compute.Band(b["min_score"], b["grade"], b["remark"], b["is_fail"])
        for b in snapshot["grading_key"]
    ]


def _subject_row(snapshot, subject_id):
    """``(index, row)`` of *subject_id* on the snapshot, else None."""
    for index, row in enumerate(snapshot["subjects"]):
        if row["subject"]["id"] == subject_id:
            return index, row
    return None


def _cells_in_order(row, components) -> list:
    """The row's ``(mark, marker)`` pairs in the order of *components*."""
    stored = {cell["component"]: (cell["mark"], cell["marker"]) for cell in row["cells"]}
    return [stored.get(component.pk, (None, None)) for component in components]


def _cell_list(components, pairs) -> list:
    """Cells as the amendment stores and serves them: ``[{component, mark, marker}]``."""
    return [
        {"component": component.pk, "mark": mark, "marker": marker}
        for component, (mark, marker) in zip(components, pairs)
    ]


def _overlay(components, before_pairs, clean) -> list:
    """*before_pairs* with the cleaned cells ``(student, component, mark, marker)`` laid over them."""
    changed = {component_id: (mark, marker) for _, component_id, mark, marker in clean}
    return [
        changed.get(component.pk, pair) for component, pair in zip(components, before_pairs)
    ]


def line_of(components, cells, bands) -> dict:
    """A subject's cells with the line worked out from them, for an amendment's before and after.

    *cells* is the stored ``[{component, mark, marker}]``. The grade and remark
    come from *bands*, the grading key the result was published with.
    """
    pairs = _cells_in_order({"cells": cells}, components)
    result = compute.row_result(components, pairs, bands)
    return {
        "cells": _cell_list(components, pairs),
        "total": result.total, "obtainable": result.obtainable,
        "percentage": compute.round2(result.percentage),
        "grade": result.grade, "remark": result.remark, "is_fail": result.is_fail,
    }


def _band_of(average, templates):
    """The ``band_min`` of the remark band *average* falls in, else None."""
    shown = compute.round2(average)
    if shown is None:
        return None
    for band_min, _ in templates:
        if shown >= band_min:
            return band_min
    return None


def _published_average(snapshot):
    """The overall average a snapshot carries, as a Decimal, or None when it has none."""
    text = snapshot.get("overall_average")
    return Decimal(text) if text is not None else None


def corrected_snapshot(
    snapshot, subject_id, pairs, classmates, templates, overrides, now,
) -> dict:
    """The student's snapshot as the correction to *subject_id* leaves it. Pure.

    *pairs* are the corrected ``(mark, marker)`` cells in component order,
    *classmates* the other students' snapshots as published (or as last
    corrected), *templates* the school's ``[(band_min, text)]`` highest first and
    *overrides* the class's own comment for a band. Returns a new snapshot one
    version later; *snapshot* is not changed. See the module docstring for what
    is recomputed and what is carried over.
    """
    new = copy.deepcopy(snapshot)
    components = _components(new)
    bands = _bands(new)
    index, old_row = _subject_row(new, subject_id)

    result = compute.row_result(components, pairs, bands)
    totals = [
        row["total"]
        for other in classmates
        for row in other["subjects"]
        if row["subject"]["id"] == subject_id and row["percentage"] is not None
    ]
    if result.percentage is not None:
        totals.append(result.total)
    row = publishing.subject_row(
        old_row["subject"], components, pairs, result, compute.class_stats(totals),
    )
    new["subjects"][index] = row

    new["total_obtained"], new["total_obtainable"] = publishing.snapshot_totals(new["subjects"])
    average = compute.overall_average(
        compute.row_result(components, _cells_in_order(each, components), bands).percentage
        for each in new["subjects"]
    )
    new["overall_average"] = percentage_of(average)

    if "position" in new:
        published = {
            n: _published_average(other)
            for n, other in enumerate(classmates)
            if other.get("overall_average") is not None
        }
        new["position"] = compute.positions({**published, "me": average})["me"]

    if "cumulative" in new:
        new["cumulative"] = cumulative.refreshed(
            new["cumulative"], new["term"]["id"], new["subjects"], new["overall_average"],
            [other["cumulative"] for other in classmates if "cumulative" in other],
        )

    if _band_of(_published_average(snapshot), templates) != _band_of(average, templates):
        new["principal_comment"] = publishing.principal_comment_for(average, templates, overrides)

    new["version"] = snapshot["version"] + 1
    new["amended_at"] = now.isoformat()
    return new


def reissued_cumulative(snapshot, term_id, corrected, classmates, now) -> dict | None:
    """The later term's *snapshot* with the year block refreshed for a correction to *term_id*; None when it would not change. Pure.

    *corrected* is the amended term's new snapshot for the same student and
    *classmates* the other students' snapshots of the later class-term as they
    stand. Returns a new snapshot one version later, or None when the block comes
    out the same as it was.
    """
    block = cumulative.refreshed(
        snapshot["cumulative"], term_id, corrected["subjects"], corrected["overall_average"],
        [other["cumulative"] for other in classmates if "cumulative" in other],
    )
    if block == snapshot["cumulative"]:
        return None
    new = copy.deepcopy(snapshot)
    new["cumulative"] = block
    new["version"] = snapshot["version"] + 1
    new["amended_at"] = now.isoformat()
    return new


# ── Opening what an amendment is about ─────────────────────────────────────

@dataclass
class Target:
    """A published student's result in a class and term, and the subject to correct."""

    tenant: object
    user: object
    school_class: object
    branch: object
    term: object
    class_term: object
    result: StudentTermResult
    student: Student
    subject: Subject

    @property
    def snapshot(self) -> dict:
        return self.result.snapshot


def open_target(tenant, user, class_id, term, student_id, subject_id) -> Target:
    """The published result and subject an amendment is about, for a caller who may reach the class.

    Answers 404 for a class of another school or outside the caller's branches,
    for a student the class published no result for, and for a subject that is
    not the school's; ``NOT_PUBLISHED`` for a class that has not been published
    for the term; ``SUBJECT_NOT_TAKEN`` for a subject not on the student's card.
    """
    school_class, branch = access.open_class(tenant, user, class_id)
    access.assert_term_of_class(school_class, term)
    class_term = resolve.class_term(tenant, school_class, term)
    if class_term is None or class_term.stage != StoredStage.PUBLISHED:
        raise NotPublished(NOT_PUBLISHED_FOR_AMENDMENT)
    result = (
        StudentTermResult.objects.filter(
            tenant=tenant, class_term=class_term, student_id=student_id,
            snapshot__isnull=False,
        ).select_related("student").first()
    )
    if result is None:
        raise NotFound(NO_SUCH_RESULT)
    subject = Subject.objects.filter(tenant=tenant, pk=subject_id).first()
    if subject is None:
        raise NotFound("No such subject at this school.")
    if _subject_row(result.snapshot, subject.pk) is None:
        raise SubjectNotTaken()
    return Target(
        tenant, user, school_class, branch, term, class_term, result, result.student, subject,
    )


def _pending(target: Target):
    return Amendment.objects.filter(
        tenant=target.tenant, class_term=target.class_term, student=target.student,
        subject=target.subject, status=AmendmentStatus.PENDING,
    ).first()


def form(target: Target) -> dict:
    """The amendment drawer's content: the columns and the subject's cells as published."""
    snapshot = target.snapshot
    components = _components(snapshot)
    _, row = _subject_row(snapshot, target.subject.pk)
    line = line_of(components, row["cells"], _bands(snapshot))
    pending = _pending(target)
    return {
        "student": {
            "id": target.student.pk, "name": target.student.full_name,
            "admission_number": target.student.student_number,
        },
        "subject": {"id": target.subject.pk, "name": target.subject.name},
        "class": {"id": target.school_class.pk, "name": target.school_class.name},
        "term": {"id": target.term.pk, "name": target.term.name},
        "version": target.result.version,
        "components": [
            {
                "id": c.pk, "name": c.name, "short_name": c.short_name,
                "max_score": c.max_score, "is_exam": c.is_exam,
            }
            for c in components
        ],
        **line,
        "pending": {"id": pending.pk, "raised_at": pending.raised_at} if pending else None,
    }


# ── Raising ────────────────────────────────────────────────────────────────

def raise_amendment(target: Target, reason: str, cells) -> Amendment:
    """Ask for *cells* to replace the subject's published marks; the principal decides.

    *cells* is ``[{component, mark, marker}]`` for the columns being changed.
    Refused as the module docstring says; nothing is saved on a refusal.
    """
    reason = (reason or "").strip()
    if not reason:
        raise AmendmentReasonRequired(field="reason")
    now = timezone.now()
    with transaction.atomic():
        result = StudentTermResult.objects.select_for_update().get(pk=target.result.pk)
        snapshot = result.snapshot
        components = _components(snapshot)
        _, row = _subject_row(snapshot, target.subject.pk)

        clean = sheets.check_batch(
            {target.student.pk}, components,
            [{"student": target.student.pk, **cell} for cell in cells],
            allow_blank=False,
        )
        before = _cells_in_order(row, components)
        after = _overlay(components, before, clean)
        if after == before:
            raise NothingChanged()
        if _pending(target) is not None:
            raise AmendmentPending()
        try:
            with transaction.atomic():
                amendment = Amendment.objects.create(
                    tenant=target.tenant, class_term=target.class_term,
                    student=target.student, subject=target.subject, reason=reason,
                    before=_cell_list(components, before), after=_cell_list(components, after),
                    status=AmendmentStatus.PENDING, raised_by=target.user, raised_at=now,
                )
        except IntegrityError:
            raise AmendmentPending() from None
        audit.record(
            tenant=target.tenant, user=target.user, branch=target.branch,
            entity_type="Amendment", entity_id=amendment.pk, entity_label=_label(amendment),
            summary=(
                f"{target.user.full_name} asked to correct {target.subject.name} on "
                f"{target.student.full_name}'s report card for {target.term.name}."
            ),
            action_type=AuditActionType.CREATE,
            class_id=target.school_class.pk, term_id=target.term.pk,
            student_id=target.student.pk, subject_id=target.subject.pk,
        )
    return amendment


def _label(amendment: Amendment) -> str:
    return (
        f"{amendment.subject.name}, {amendment.student.full_name}, "
        f"{amendment.class_term.term.name}"
    )


# ── Reading ────────────────────────────────────────────────────────────────

def _scoped(tenant, user):
    """The tenant's amendments in the branches the caller can see."""
    return Amendment.objects.filter(
        transaction_branch_q_for_user(user, "class_term__"), tenant=tenant,
    )


_RELATED = (
    "student", "subject", "raised_by", "decided_by",
    "class_term", "class_term__term", "class_term__branch",
    "class_term__school_class",
)


def _versions():
    """A prefetch of each amendment's versions with the result they belong to."""
    return Prefetch(
        "result_versions", queryset=ResultVersion.objects.select_related("result"),
    )


def _own_version(amendment):
    """The version of the amended student's own result the approval issued, or None while waiting.

    An approval also issues versions of the student's later cumulative cards;
    those belong to other class-terms and are not the number the amended card
    carries. Call on an amendment loaded with :func:`_versions`.
    """
    return next(
        (
            each for each in amendment.result_versions.all()
            if each.result.class_term_id == amendment.class_term_id
        ),
        None,
    )


def listing(tenant, user, term, branch, status):
    """The amendments of *term* in the caller's branches, waiting ones first, newest first.

    A queryset: the view paginates it and :func:`rows_payload` renders the page.
    A *status* that is not one of the three is refused.
    """
    query = (
        _scoped(tenant, user).filter(class_term__term=term)
        .select_related(*_RELATED).prefetch_related(_versions())
    )
    if branch is not None:
        query = query.filter(class_term__branch=branch)
    if status:
        if status not in AmendmentStatus.values:
            raise ValidationError(
                {"status": ["Choose PENDING, APPROVED or REJECTED, or leave it out."]},
            )
        query = query.filter(status=status)
    return query.annotate(
        waiting_first=Case(
            When(status=AmendmentStatus.PENDING, then=0), default=1, output_field=IntegerField(),
        ),
    ).order_by("waiting_first", "-raised_at", "-pk")


def _frames(tenant, amendments) -> dict:
    """``{(class_term_id, student_id): (components, bands)}`` the published results of *amendments* used.

    One query for the page, reading only the two parts of each snapshot the
    arithmetic needs.
    """
    pairs = {(a.class_term_id, a.student_id) for a in amendments}
    if not pairs:
        return {}
    rows = StudentTermResult.objects.filter(
        tenant=tenant, class_term_id__in={p[0] for p in pairs},
        student_id__in={p[1] for p in pairs}, snapshot__isnull=False,
    ).values_list("class_term_id", "student_id", "snapshot__components", "snapshot__grading_key")
    frames = {}
    for class_term_id, student_id, components, key in rows:
        if (class_term_id, student_id) in pairs:
            frames[(class_term_id, student_id)] = (
                _components({"components": components}),
                _bands({"grading_key": key}),
            )
    return frames


def rows_payload(tenant, amendments, user=None) -> dict:
    """``{"branch_dimension": bool, "rows": [...]}`` for *amendments*, before Field Access is applied.

    ``can_decide`` is true for a correction still waiting that *user* did not
    raise; whether they hold the key to decide is the screen's to ask.
    """
    several = only_branch_id_or_several(tenant) is None
    frames = _frames(tenant, amendments)
    rows = []
    for found in amendments:
        school_class, student = found.class_term.school_class, found.student
        components, bands = frames.get((found.class_term_id, found.student_id), ([], []))
        version = _own_version(found)
        rows.append({
            "id": found.pk, "status": found.status,
            "student": {
                "id": student.pk, "name": student.full_name,
                "admission_number": student.student_number,
            },
            "class": {"id": school_class.pk, "name": school_class.name},
            "term": {"id": found.class_term.term_id, "name": found.class_term.term.name},
            "subject": {"id": found.subject_id, "name": found.subject.name},
            "branch": (
                {"id": found.class_term.branch_id, "name": found.class_term.branch.name}
                if several else None
            ),
            "amendment_reason": found.reason,
            "raised_by": found.raised_by.full_name if found.raised_by else None,
            "raised_at": found.raised_at,
            "decided_by": found.decided_by.full_name if found.decided_by else None,
            "decided_at": found.decided_at,
            "decision_note": found.decision_note,
            "version": version.version if version is not None else None,
            "before": line_of(components, found.before, bands),
            "after": line_of(components, found.after, bands),
            "can_decide": (
                user is not None and found.status == AmendmentStatus.PENDING
                and found.raised_by_id != user.pk
            ),
        })
    return {"branch_dimension": several, "rows": rows}


def one_payload(tenant, amendment, user=None) -> dict:
    """*amendment* as a list row, for the answer to raising or deciding it."""
    found = (
        Amendment.objects.select_related(*_RELATED).prefetch_related(_versions())
        .get(pk=amendment.pk)
    )
    return rows_payload(tenant, [found], user)["rows"][0]


# ── Deciding ───────────────────────────────────────────────────────────────

def open_for_decision(tenant, user, amendment_id) -> Amendment:
    """The amendment, for a caller who reaches its branch and did not raise it.

    A 404 for another school's amendment and one outside the caller's branches,
    before anything else is said. ``SELF_DECISION`` for the person who asked for
    the correction.
    """
    found = (
        _scoped(tenant, user).filter(pk=amendment_id).select_related(*_RELATED).first()
    )
    if found is None:
        raise NotFound(NO_SUCH_AMENDMENT)
    if found.raised_by_id is not None and found.raised_by_id == user.pk:
        raise SelfDecision()
    return found


def _classmates(result) -> list:
    """The snapshots of every other student the class published, as they stand."""
    return [
        snapshot
        for snapshot in StudentTermResult.objects.filter(
            tenant_id=result.tenant_id, class_term_id=result.class_term_id,
            snapshot__isnull=False,
        ).exclude(pk=result.pk).values_list("snapshot", flat=True)
    ]


def approve(tenant, user, amendment: Amendment, note: str) -> Amendment:
    """Approve *amendment*: correct the sheet's marks and issue the student's next version.

    The caller has been through :func:`open_for_decision`. Refused with
    ``NOT_PENDING`` once decided. The guardian is told after commit when the
    result was released.
    """
    now = timezone.now()
    with transaction.atomic():
        result, later = _lock_results(tenant, amendment)
        locked = Amendment.objects.select_for_update().get(pk=amendment.pk)
        if locked.status != AmendmentStatus.PENDING:
            raise NotPending()

        snapshot = result.snapshot
        components = _components(snapshot)
        pairs = _cells_in_order({"cells": locked.after}, components)

        sheet = ScoreSheet.objects.get(
            tenant=tenant, class_term_id=locked.class_term_id, subject_id=locked.subject_id,
        )
        _write_scores(tenant, sheet, locked, components, pairs, now)

        templates = [
            (row["band_min"], row["text"]) for row in setup.templates_payload(tenant)["templates"]
        ]
        overrides = {
            row.band_min: row.text
            for row in amendment.class_term.principal_comments.all()
        }
        corrected = corrected_snapshot(
            snapshot, locked.subject_id, pairs, _classmates(result), templates, overrides, now,
        )
        ResultVersion.objects.get_or_create(
            tenant=tenant, result=result, version=snapshot["version"],
            defaults={"snapshot": snapshot},
        )
        result.snapshot, result.version = corrected, corrected["version"]
        result.save(update_fields=["snapshot", "version", "updated_at"])
        ResultVersion.objects.create(
            tenant=tenant, result=result, version=corrected["version"],
            snapshot=corrected, amendment=locked,
        )

        reissued = reissue_later_cards(tenant, locked, corrected, later, now)

        _close(locked, AmendmentStatus.APPROVED, user, note, now)
        audit.record(
            tenant=tenant, user=user, branch=amendment.class_term.branch,
            entity_type="Amendment", entity_id=locked.pk, entity_label=_label(amendment),
            summary=(
                f"{user.full_name} approved the correction to {amendment.subject.name} on "
                f"{amendment.student.full_name}'s report card for "
                f"{amendment.class_term.term.name}; it is now version {corrected['version']}."
            ),
            class_id=amendment.class_term.school_class_id,
            term_id=amendment.class_term.term_id,
            student_id=amendment.student_id, subject_id=amendment.subject_id,
            decision="approved", version=corrected["version"],
            later_versions={each.class_term_id: each.version for each in reissued},
        )
        for each in [result, *reissued]:
            if each.released_at is not None:
                publishing.send_updated_after_commit(tenant, each.class_term_id, each.student_id)
    return locked


def _lock_results(tenant, amendment):
    """``(amended result, later results)`` of the student, all locked in result-id order.

    The later results are the student's published cards of the amendment's
    school year after the amended term that carry a cumulative block, in term
    order. One locking query, ordered by id, takes them with the amended result.
    """
    terms = cumulative.session_terms(tenant, amendment.class_term.term)
    position = next(n for n, each in enumerate(terms) if each.pk == amendment.class_term.term_id)
    later_ids = [each.pk for each in terms[position + 1:]]
    rows = list(
        StudentTermResult.objects.select_for_update(of=("self",))
        .select_related("class_term")
        .filter(
            Q(class_term_id=amendment.class_term_id)
            | Q(
                class_term__term_id__in=later_ids, class_term__stage=StoredStage.PUBLISHED,
                snapshot__isnull=False,
            ),
            tenant=tenant, student_id=amendment.student_id,
        )
        .order_by("pk")
    )
    order = {term_id: n for n, term_id in enumerate(later_ids)}
    result = next(row for row in rows if row.class_term_id == amendment.class_term_id)
    later = sorted(
        (row for row in rows if row is not result and "cumulative" in (row.snapshot or {})),
        key=lambda row: (order[row.class_term.term_id], row.pk),
    )
    return result, later


def reissue_later_cards(tenant, amendment, corrected, later, now) -> list:
    """Reissue each of the student's *later* cumulative cards for the correction; the results reissued.

    *corrected* is the amended term's new snapshot. Each card that changes is
    saved as its next version with the one it replaces kept, both linked to
    *amendment*. No classmate's card is written.
    """
    term_id = amendment.class_term.term_id
    reissued = []
    for row in later:
        snapshot = row.snapshot
        new = reissued_cumulative(snapshot, term_id, corrected, _classmates(row), now)
        if new is None:
            continue
        ResultVersion.objects.get_or_create(
            tenant=tenant, result=row, version=snapshot["version"],
            defaults={"snapshot": snapshot},
        )
        row.snapshot, row.version = new, new["version"]
        row.save(update_fields=["snapshot", "version", "updated_at"])
        ResultVersion.objects.create(
            tenant=tenant, result=row, version=new["version"], snapshot=new,
            amendment=amendment,
        )
        reissued.append(row)
    return reissued


def _write_scores(tenant, sheet, amendment, components, pairs, now) -> None:
    """Lay the corrected cells over the student's score rows on *sheet*."""
    existing = {
        row.component_id: row
        for row in Score.objects.filter(
            tenant=tenant, sheet=sheet, student_id=amendment.student_id,
        )
    }
    create, update = [], []
    for component, (mark, marker) in zip(components, pairs):
        row = existing.get(component.pk)
        if row is None:
            create.append(Score(
                tenant=tenant, sheet=sheet, student_id=amendment.student_id,
                component_id=component.pk, mark=mark, marker=marker,
                entered_by=amendment.raised_by, entered_at=now,
                amended_by=amendment.raised_by, amended_at=now,
            ))
        elif (row.mark, row.marker) != (mark, marker):
            row.mark, row.marker = mark, marker
            row.amended_by, row.amended_at, row.updated_at = amendment.raised_by, now, now
            update.append(row)
    if create:
        Score.objects.bulk_create(create)
    if update:
        Score.objects.bulk_update(update, ["mark", "marker", "amended_by", "amended_at", "updated_at"])


def _close(amendment: Amendment, status, user, note, now) -> None:
    amendment.status = status
    amendment.decided_by, amendment.decided_at = user, now
    amendment.decision_note = (note or "").strip()
    amendment.save(update_fields=[
        "status", "decided_by", "decided_at", "decision_note", "updated_at",
    ])


def reject(tenant, user, amendment: Amendment, note: str) -> Amendment:
    """Reject *amendment* with *note*; the result and the sheet are left exactly as they were."""
    note = (note or "").strip()
    if not note:
        raise NoteRequired("Say why this correction is being rejected.", field="note")
    now = timezone.now()
    with transaction.atomic():
        locked = Amendment.objects.select_for_update().get(pk=amendment.pk)
        if locked.status != AmendmentStatus.PENDING:
            raise NotPending()
        _close(locked, AmendmentStatus.REJECTED, user, note, now)
        audit.record(
            tenant=tenant, user=user, branch=amendment.class_term.branch,
            entity_type="Amendment", entity_id=locked.pk, entity_label=_label(amendment),
            summary=(
                f"{user.full_name} rejected the correction to {amendment.subject.name} on "
                f"{amendment.student.full_name}'s report card for "
                f"{amendment.class_term.term.name}."
            ),
            class_id=amendment.class_term.school_class_id,
            term_id=amendment.class_term.term_id,
            student_id=amendment.student_id, subject_id=amendment.subject_id,
            decision="rejected",
        )
    return locked


# ── A student's history ────────────────────────────────────────────────────

_WORDING = {
    AmendmentStatus.PENDING: "Correction to {subject} asked for, {term}",
    AmendmentStatus.APPROVED: "{subject} corrected for {term}",
    AmendmentStatus.REJECTED: "Correction to {subject} rejected for {term}",
}


def history_entries(tenant, student) -> list:
    """The student's amendments as Results tab history entries, before Field Access is applied.

    One entry for each amendment, whatever its status. Its time is when it was
    last acted on: decided if it was, otherwise raised.
    """
    entries = []
    for found in (
        Amendment.objects.filter(tenant=tenant, student=student)
        .select_related("subject", "class_term__term", "raised_by", "decided_by")
        .prefetch_related(_versions())
    ):
        decided = found.status != AmendmentStatus.PENDING
        actor = found.decided_by if decided else found.raised_by
        version = _own_version(found)
        entries.append({
            "kind": HistoryKind.AMENDMENT,
            "text": _WORDING[found.status].format(
                subject=found.subject.name, term=found.class_term.term.name,
            ),
            "by": actor.full_name if actor else None,
            "at": found.decided_at if decided else found.raised_at,
            "amendment": {
                "id": found.pk, "status": found.status,
                "subject": {"id": found.subject_id, "name": found.subject.name},
                "term": {"id": found.class_term.term_id, "name": found.class_term.term.name},
                "amendment_reason": found.reason,
                "raised_by": found.raised_by.full_name if found.raised_by else None,
                "raised_at": found.raised_at,
                "decided_by": found.decided_by.full_name if found.decided_by else None,
                "decided_at": found.decided_at,
                "decision_note": found.decision_note,
                "version": version.version if version is not None else None,
            },
        })
    return entries
