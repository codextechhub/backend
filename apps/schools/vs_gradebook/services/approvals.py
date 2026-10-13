"""The principal's part: the queue of submitted classes, returning one, and the class's comments.

**The queue** lists, for a term, the classes that have been submitted to the
principal (AWAITING) or published, in the branches the caller can see. Each row
says how many of its subjects are in, who the class teacher is and, for the
confirmation the approver reads before publishing, which students' results are
held and who held them. It does not say why: a hold's reason is the bursar's
note and stays on the fee-holds screen. Only a class awaiting approval can be
decided (``can_decide``).

**Returning** sends a submitted class back. Without a subject it goes to its
class teacher: the stage is cleared, who returned it, when and the note are
kept on the class, and the class teacher is told in the app. With a subject, that
one sheet goes back to its main teacher as RETURNED with the note and the
class's stage is cleared as well, so the class cannot be published while a sheet
is open again; the main teacher is told in the app as for a moderated sheet.
Nothing is deleted. Only a class awaiting approval can be returned
(``NOT_AWAITING``), and the note is required (``NOTE_REQUIRED``).

**The principal's comments** are written once per class for each band of the
school's remark templates, starting from the template's sentence. A student's
card prints the comment for the band their overall average falls in, the class's
own comment if one was written and the template otherwise. They can be written
and revised until the class is published, and not after (``CLASS_LOCKED``).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType
from vs_notifications.notify import send_notification
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import EVENT_CLASS_RETURNED, SheetStatus, StoredStage
from ..exceptions import (
    ClassLocked,
    CommentRequired,
    NoteRequired,
    NotAwaiting,
    NotSubmitted,
    UnknownBand,
)
from ..models import ClassTerm, PrincipalComment, ResultHold, ScoreSheet
from . import audit, book, hubs, moderation, resolve, setup, sheets
from .classbook import ClassContext

ON_THE_QUEUE = (StoredStage.AWAITING, StoredStage.PUBLISHED)


def _person(staff) -> dict | None:
    return {"id": staff.pk, "name": staff.user.full_name} if staff is not None else None


def _label(ctx: ClassContext) -> str:
    return f"{ctx.school_class.name}, {ctx.term.name}"


# ── The queue ──────────────────────────────────────────────────────────────

def held_students(tenant, term, entries) -> dict:
    """``{class_id: [{student, held_by, placed_at}]}`` for the open holds on the entries' rolls."""
    open_holds = {
        hold.student_id: hold
        for hold in ResultHold.objects.filter(
            tenant=tenant, term=term, lifted_at__isnull=True,
        ).select_related("placed_by")
    }
    out = {}
    for entry in entries:
        rows = []
        for roll_entry in entry.roll:
            hold = open_holds.get(roll_entry.student.pk)
            if hold is None:
                continue
            rows.append({
                "student": {"id": roll_entry.student.pk, "name": roll_entry.student.full_name},
                "held_by": hold.placed_by.full_name if hold.placed_by is not None else None,
                "placed_at": hold.placed_at,
            })
        out[entry.school_class.pk] = rows
    return out


def queue(tenant, user, term, branch) -> dict:
    """The approvals queue for *term*: ``{"branch_dimension": bool, "classes": [...]}``."""
    placed, _, branches, _ = hubs.reachable_classes(tenant, user, term, branch)
    several = len(branches) > 1
    entries = [entry for entry in book.load(tenant, term, placed) if entry.stage in ON_THE_QUEUE]
    held = held_students(tenant, term, entries)
    rows = []
    for entry in entries:
        school_class, home, class_term = entry.school_class, entry.home, entry.class_term
        rows.append({
            "class": {"id": school_class.pk, "name": school_class.name},
            "branch": {"id": home.pk, "name": home.name} if several else None,
            "class_teacher": _person(school_class.class_teacher),
            "subjects_in": entry.subjects_in, "subjects_due": entry.subjects_due,
            "stage": entry.stage,
            "submitted_at": class_term.submitted_at,
            "published_at": class_term.published_at,
            "held": held[school_class.pk],
            "can_decide": entry.stage == StoredStage.AWAITING,
        })
    return {"branch_dimension": several, "classes": rows}


# ── Returning ──────────────────────────────────────────────────────────────

def _awaiting(ctx: ClassContext) -> ClassTerm:
    """The class's term under a row lock, refused unless it is awaiting the principal."""
    class_term = (
        ClassTerm.objects.select_for_update()
        .filter(tenant=ctx.tenant, school_class=ctx.school_class, term=ctx.term).first()
    )
    if class_term is None or class_term.stage != StoredStage.AWAITING:
        raise NotAwaiting()
    return class_term


def return_class(ctx: ClassContext, note: str, subject_id=None) -> None:
    """Send the class, or one of its sheets when *subject_id* is given, back with *note*."""
    note = (note or "").strip()
    if not note:
        raise NoteRequired(
            "Say what needs correcting before returning the class.", field="note",
        )
    sheet_ctx = (
        sheets.load(
            ctx.tenant, ctx.user, ctx.school_class.pk, subject_id, ctx.term, rule=sheets.ANY,
        )
        if subject_id is not None else None
    )
    now = timezone.now()
    with transaction.atomic():
        class_term = _awaiting(ctx)
        if sheet_ctx is None:
            class_term.stage = None
            class_term.returned_by, class_term.returned_at = ctx.user, now
            class_term.returned_note = note
            summary = f"{ctx.user.full_name} returned {_label(ctx)} to its class teacher."
        else:
            sheet = (
                ScoreSheet.objects.select_for_update().get(pk=sheet_ctx.sheet.pk)
                if sheet_ctx.sheet is not None else None
            )
            if sheet is None or sheet.status not in (SheetStatus.SUBMITTED, SheetStatus.APPROVED):
                raise NotSubmitted()
            sheet.status = SheetStatus.RETURNED
            sheet.returned_by, sheet.returned_at, sheet.returned_note = ctx.user, now, note
            sheet.save(update_fields=[
                "status", "returned_by", "returned_at", "returned_note", "updated_at",
            ])
            class_term.stage = None
            class_term.returned_by, class_term.returned_at, class_term.returned_note = None, None, ""
            summary = (
                f"{ctx.user.full_name} returned {sheets.label(sheet_ctx)} to its teacher."
            )
        class_term.save(update_fields=[
            "stage", "returned_by", "returned_at", "returned_note", "updated_at",
        ])
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="ClassTerm", entity_id=class_term.pk, entity_label=_label(ctx),
            summary=summary, class_id=ctx.school_class.pk, term_id=ctx.term.pk,
            subject_id=subject_id, decision="returned",
        )
    if sheet_ctx is None:
        _tell_class_teacher(ctx, note)
    else:
        moderation.tell_the_teacher(sheet_ctx, note)


def _tell_class_teacher(ctx: ClassContext, note: str) -> None:
    """Tell the class teacher in the app that the class came back, when they can be reached."""
    staff = ctx.school_class.class_teacher
    if staff is None or not staff.is_on_roll or not staff.user.may_sign_in:
        return
    several = only_branch_id_or_several(ctx.tenant) is None
    send_notification(
        event_key=EVENT_CLASS_RETURNED,
        context={
            "teacher_name": staff.user.full_name,
            "returned_by_name": ctx.user.full_name,
            "class_name": ctx.school_class.name,
            "term_name": ctx.term.name,
            "note": note,
            "branch_name": ctx.branch.name if several else "",
            "tenant_name": ctx.tenant.name,
        },
        recipients=[staff.user],
        tenant=ctx.tenant,
        branch=ctx.branch,
    )


# ── The principal's comments ───────────────────────────────────────────────

def principal_payload(ctx: ClassContext) -> dict:
    """Each remark template band with the class's own comment for it, before Field Access."""
    class_term = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term)
    overrides = (
        {
            row.band_min: row
            for row in PrincipalComment.objects.filter(
                tenant=ctx.tenant, class_term=class_term,
            ).select_related("written_by")
        }
        if class_term is not None else {}
    )
    stage = class_term.stage if class_term is not None else None
    bands = []
    for template in setup.templates_payload(ctx.tenant)["templates"]:
        found = overrides.get(template["band_min"])
        bands.append({
            "band_min": template["band_min"], "template": template["text"],
            "override": (
                {
                    "text": found.text,
                    "written_by": found.written_by.full_name if found.written_by else None,
                    "written_at": found.written_at,
                }
                if found is not None else None
            ),
        })
    return {
        "class": {"id": ctx.school_class.pk, "name": ctx.school_class.name},
        "term": {"id": ctx.term.pk, "name": ctx.term.name},
        "locked": stage == StoredStage.PUBLISHED,
        "bands": bands,
    }


def save_principal_comments(ctx: ClassContext, comments) -> None:
    """Write the class's comment for each band in *comments* (``[{band_min, text}]``).

    Bands not named keep what they have. A band the school's templates do not
    list, or a blank text, refuses the whole request; nothing is written.
    """
    known = {row["band_min"] for row in setup.templates_payload(ctx.tenant)["templates"]}
    unknown = sorted({row["band_min"] for row in comments} - known)
    if unknown:
        raise UnknownBand(
            f"{', '.join(str(band) for band in unknown)} "
            f"{'is not a band' if len(unknown) == 1 else 'are not bands'} in the school's "
            f"remark templates.",
            field="comments", bands=unknown,
        )
    clean = []
    for row in comments:
        text = (row["text"] or "").strip()
        if not text:
            raise CommentRequired(
                "Write the comment for every band you are saving.", field="comments",
                band_min=row["band_min"],
            )
        clean.append((row["band_min"], text))

    existing = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term)
    if existing is not None and existing.stage == StoredStage.PUBLISHED:
        raise ClassLocked("This class's results are published, so the comments are final.")
    now = timezone.now()
    with transaction.atomic():
        class_term = resolve.class_term(ctx.tenant, ctx.school_class, ctx.term, create=True)
        class_term = ClassTerm.objects.select_for_update().get(pk=class_term.pk)
        if class_term.stage == StoredStage.PUBLISHED:
            raise ClassLocked("This class's results are published, so the comments are final.")
        for band_min, text in clean:
            PrincipalComment.objects.update_or_create(
                tenant=ctx.tenant, class_term=class_term, band_min=band_min,
                defaults={"text": text, "written_by": ctx.user, "written_at": now},
            )
        audit.record(
            tenant=ctx.tenant, user=ctx.user, branch=ctx.branch,
            entity_type="ClassTerm", entity_id=class_term.pk, entity_label=_label(ctx),
            summary=f"{ctx.user.full_name} wrote the principal's comments for {_label(ctx)}.",
            class_id=ctx.school_class.pk, term_id=ctx.term.pk,
            bands=[band for band, _ in clean], action_type=AuditActionType.UPDATE,
        )
