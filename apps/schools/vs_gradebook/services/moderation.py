"""Moderation: the head of a department decides on a sheet before it counts.

A sheet is moderated when it is submitted, the class's programme is one the
school moderates and the subject's department has a head
(:func:`.sheets.routes_to_moderation`). It then waits for that head, who either
**approves** it (it is in) or **returns** it to its main teacher with a note.

**The queue** lists the moderated sheets of a term in the branches the caller
can see. The head of a department sees the sheets of the subjects in the
departments they head and can decide on those that are waiting. A holder of
``hub.view`` sees every moderated sheet in their branches and can decide on none
of them unless they also head the department; that is what ``can_decide`` says.

**Each row's figures** are worked out from the stored scores exactly as the
sheet itself is (:mod:`.figures`): the class average is the mean percentage of
the students whose row is graded, the highest and lowest are totals, the pass
rate is the whole percentage of graded rows outside a fail band, and ``marked``
is how many of the sheet's roll have a complete row. ``compare`` names the
other classes of the same programme with a sheet of the same subject this term,
so a head can see whether one arm is marked harder than the others.

**Deciding** needs the key ``moderation.approve`` and that the caller heads the
department; only a submitted sheet waiting for moderation can be decided. A
return needs a note, reopens the sheet for the main teacher and tells them so
in the app.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from schools.vs_academics.services import heads
from vs_audit.models import AuditActionType
from vs_notifications.notify import send_notification
from vs_rbac.scoping import branch_q_for_user, only_branch_id_or_several

from ..constants import EVENT_SHEET_RETURNED, SetupStatus, SheetStatus
from ..exceptions import NoteRequired, NotSubmitted
from ..models import AssessmentComponent, GradingScale, ScoreSheet
from . import access, audit, book, compute, figures, resolve
from .sheets import SheetContext, label


def _components_by_structure(tenant, structure_ids) -> dict:
    out: dict = {}
    for component in AssessmentComponent.objects.filter(
        tenant=tenant, structure_id__in=list(structure_ids),
    ).order_by("order", "id"):
        out.setdefault(component.structure_id, []).append(component)
    return out


def _scales_for(tenant, classes_with_branch) -> dict:
    """``{class_id: [GradeBand]}``: each class's scale in force, in one query for all of them."""
    program_ids = {school_class.level.program_id for school_class, _ in classes_with_branch}
    candidates = list(
        GradingScale.objects.filter(
            tenant=tenant, program_id__in=program_ids, status=SetupStatus.IN_USE,
        ).prefetch_related("bands")
    )
    out = {}
    for school_class, home in classes_with_branch:
        program_id = school_class.level.program_id
        own = [c for c in candidates if c.program_id == program_id and c.branch_id == home.pk]
        shared = [c for c in candidates if c.program_id == program_id and c.branch_id is None]
        chosen = (own or shared or [None])[0]
        out[school_class.pk] = list(chosen.bands.all()) if chosen is not None else []
    return out


def queue(tenant, user, term, branch, *, sees_every_sheet: bool) -> dict:
    """The moderation queue for *term*: ``{"branch_dimension": bool, "sheets": [...]}``.

    *sees_every_sheet* is whether the caller holds ``hub.view``, which widens
    the queue from the departments they head to every moderated sheet in their
    branches.
    """
    headed = set(heads.departments_headed_by(user, tenant).values_list("pk", flat=True))
    query = ScoreSheet.objects.filter(
        branch_q_for_user(user, "class_term__"),
        tenant=tenant, class_term__term=term, needs_moderation=True,
    ).select_related(
        "class_term", "class_term__branch", "class_term__school_class",
        "class_term__school_class__level", "subject", "subject__department",
    )
    if branch is not None:
        query = query.filter(class_term__branch=branch)
    if not sees_every_sheet:
        query = query.filter(subject__department_id__in=headed)
    sheets = list(query.order_by("subject__name", "class_term__school_class__name", "pk"))

    several = only_branch_id_or_several(tenant) is None
    placed = {}
    for sheet in sheets:
        placed[sheet.class_term.school_class_id] = (
            sheet.class_term.school_class, sheet.class_term.branch,
        )
    placed_list = list(placed.values())
    class_ids = list(placed)

    rolls = book.rolls_for(tenant, term, placed_list)
    takers = book.takers_for(tenant, term, class_ids)
    leads = book.leads_for(tenant, term, class_ids)
    scores = figures.stored_scores(tenant, [sheet.pk for sheet in sheets])
    components = _components_by_structure(
        tenant, {s.class_term.structure_id for s in sheets if s.class_term.structure_id},
    )
    bands = _scales_for(tenant, placed_list)

    rows = []
    for sheet in sheets:
        school_class = sheet.class_term.school_class
        roll = [entry.student for entry in rolls.get(school_class.pk, [])]
        taking = takers.get(school_class.pk, {}).get(sheet.subject_id)
        if taking:
            roll = [student for student in roll if student.pk in taking]
        lines = figures.lines(
            components.get(sheet.class_term.structure_id, []), bands.get(school_class.pk, []),
            roll, scores.get(sheet.pk, {}),
        )
        found = compute.sheet_figures(line.result for line in lines)
        teacher = leads.get((school_class.pk, sheet.subject_id))
        rows.append({
            "class": {"id": school_class.pk, "name": school_class.name},
            "subject": {"id": sheet.subject_id, "name": sheet.subject.name},
            "branch": (
                {"id": sheet.class_term.branch_id, "name": sheet.class_term.branch.name}
                if several else None
            ),
            "main_teacher": (
                {"id": teacher.pk, "name": teacher.user.full_name} if teacher is not None else None
            ),
            "marked": sum(1 for line in lines if line.result.complete),
            "roll": len(lines),
            "class_average": compute.round2(found.average),
            "highest": found.highest, "lowest": found.lowest, "pass_rate": found.pass_rate,
            "status": sheet.status,
            "compare": [],
            "can_decide": (
                sheet.status == SheetStatus.SUBMITTED
                and sheet.subject.department_id in headed
            ),
        })
    _name_the_other_arms(tenant, user, term, sheets, rows)
    return {"branch_dimension": several, "sheets": rows}


def _name_the_other_arms(tenant, user, term, sheets, rows) -> None:
    """Fill each row's ``compare`` with the other classes of its programme that have the same subject's sheet."""
    if not sheets:
        return
    subject_ids = {sheet.subject_id for sheet in sheets}
    program_ids = {sheet.class_term.school_class.level.program_id for sheet in sheets}
    peers = (
        ScoreSheet.objects.filter(
            branch_q_for_user(user, "class_term__"),
            tenant=tenant, class_term__term=term, subject_id__in=subject_ids,
            class_term__school_class__level__program_id__in=program_ids,
        )
        .select_related("class_term__school_class", "class_term__school_class__level")
        .order_by("class_term__school_class__name")
    )
    arms: dict = {}
    for peer in peers:
        school_class = peer.class_term.school_class
        arms.setdefault((peer.subject_id, school_class.level.program_id), []).append(school_class)
    for sheet, row in zip(sheets, rows):
        this = sheet.class_term.school_class
        row["compare"] = [
            other.name
            for other in arms.get((sheet.subject_id, this.level.program_id), [])
            if other.pk != this.pk
        ]


# ── Deciding ───────────────────────────────────────────────────────────────

def _waiting(ctx: SheetContext) -> ScoreSheet:
    """The sheet under a row lock, refused unless it is submitted and waiting for its head."""
    if ctx.sheet is None:
        raise NotSubmitted()
    sheet = ScoreSheet.objects.select_for_update().get(pk=ctx.sheet.pk)
    if sheet.status != SheetStatus.SUBMITTED or not sheet.needs_moderation:
        raise NotSubmitted()
    return sheet


@transaction.atomic
def approve(ctx: SheetContext) -> None:
    """Approve the sheet: it is in, and the head's name and time are kept."""
    access.assert_heads_department(ctx.user, ctx.tenant, ctx.subject)
    sheet = _waiting(ctx)
    sheet.status = SheetStatus.APPROVED
    sheet.moderated_by, sheet.moderated_at = ctx.user, timezone.now()
    sheet.save(update_fields=["status", "moderated_by", "moderated_at", "updated_at"])
    audit.record(
        tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
        entity_type="ScoreSheet", entity_id=sheet.pk, entity_label=label(ctx),
        action_type=AuditActionType.UPDATE,
        summary=f"{ctx.user.full_name} approved {label(ctx)}.",
        class_id=ctx.school_class.pk, subject_id=ctx.subject.pk, term_id=ctx.term.pk,
        decision="approved",
    )


@transaction.atomic
def return_sheet(ctx: SheetContext, note: str) -> None:
    """Return the sheet to its main teacher with *note*, reopening it for correction.

    Nothing is deleted. The main teacher is told in the app when they can be
    reached; a sheet with no main teacher is still returned.
    """
    access.assert_heads_department(ctx.user, ctx.tenant, ctx.subject)
    note = (note or "").strip()
    if not note:
        raise NoteRequired("Say what needs correcting before returning the sheet.", field="note")
    sheet = _waiting(ctx)
    sheet.status = SheetStatus.RETURNED
    sheet.returned_by, sheet.returned_at, sheet.returned_note = ctx.user, timezone.now(), note
    sheet.save(update_fields=[
        "status", "returned_by", "returned_at", "returned_note", "updated_at",
    ])
    audit.record(
        tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
        entity_type="ScoreSheet", entity_id=sheet.pk, entity_label=label(ctx),
        action_type=AuditActionType.UPDATE,
        summary=f"{ctx.user.full_name} returned {label(ctx)} to its teacher.",
        class_id=ctx.school_class.pk, subject_id=ctx.subject.pk, term_id=ctx.term.pk,
        decision="returned",
    )
    tell_the_teacher(ctx, note)


def tell_the_teacher(ctx: SheetContext, note: str) -> None:
    """Tell the sheet's main teacher in the app that it was returned with *note*, when they can be reached."""
    teacher = resolve.main_teacher(ctx.school_class, ctx.subject, ctx.school_class.session_id)
    if teacher is None or not teacher.is_on_roll or not teacher.user.may_sign_in:
        return
    several = only_branch_id_or_several(ctx.tenant) is None
    send_notification(
        event_key=EVENT_SHEET_RETURNED,
        context={
            "teacher_name": teacher.user.full_name,
            "returned_by_name": ctx.user.full_name,
            "subject_name": ctx.subject.name,
            "class_name": ctx.school_class.name,
            "term_name": ctx.term.name,
            "note": note,
            "branch_name": ctx.branch.name if several else "",
            "tenant_name": ctx.tenant.name,
        },
        recipients=[teacher.user],
        tenant=ctx.tenant,
        branch=ctx.branch,
    )
