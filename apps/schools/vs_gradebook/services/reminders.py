"""Reminding a sheet's main teacher that it is not in.

An administrator who sees a late sheet on the results hub sends its main teacher
a reminder, in the app and by email. **One reminder per sheet per school day**,
so a row that reads "Reminded" stays reminded and two administrators working the
same list do not nag the same teacher.

A reminder goes to the sheet's main teacher only, and only when there is one who
is on the staff roll and able to sign in. Otherwise there is nobody to remind,
and the refusal says which of the three it is (no main teacher, left the school,
cannot sign in) so the administrator knows whom to contact instead, rather than
quietly reminding someone else. A sheet that is already in has nothing to remind
about.

A sheet nobody has started has no row yet; a reminder creates it, because the
reminder is recorded against the sheet.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from vs_audit.models import AuditActionType
from vs_config.clock import branch_today
from vs_notifications.notify import send_notification
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import EVENT_SHEET_REMINDER
from ..exceptions import AlreadyReminded, NoMainTeacher, SheetLocked, TeacherUnreachable
from ..models import SheetReminder
from . import audit, resolve
from .sheets import SheetContext, ensure_sheet, label, lock_error

ALREADY_IN = "This sheet is already in, so there is nothing to remind the teacher about."


def _reachable_teacher(ctx: SheetContext):
    """The main teacher's staff record, or the refusal that says why there is nobody to remind."""
    teacher = resolve.main_teacher(ctx.school_class, ctx.subject, ctx.school_class.session_id)
    if teacher is None:
        raise NoMainTeacher()
    name = teacher.user.full_name
    if not teacher.is_on_roll:
        raise TeacherUnreachable(
            f"{name} has left the school, so a reminder would not reach anyone. "
            f"Assign another main teacher in Staff Management.",
        )
    if not teacher.user.may_sign_in:
        raise TeacherUnreachable(
            f"{name} cannot sign in, so a reminder would not reach them. Ask an "
            f"administrator to restore their account, or contact them another way.",
        )
    return teacher


@transaction.atomic
def remind(ctx: SheetContext, sender) -> SheetReminder:
    """Send the sheet's main teacher a reminder for today.

    Refused for a sheet that is in, a sheet with nobody to remind, and a sheet
    already reminded today at its branch.
    """
    if lock_error(ctx) is not None:
        raise SheetLocked(ALREADY_IN)
    teacher = _reachable_teacher(ctx)
    teacher_user = teacher.user
    _, sheet = ensure_sheet(ctx, fix_structure=False)

    today = branch_today(ctx.tenant, ctx.branch)
    try:
        with transaction.atomic():
            reminder = SheetReminder.objects.create(
                tenant=ctx.tenant, sheet=sheet, on_date=today, sent_by=sender,
            )
    except IntegrityError:
        raise AlreadyReminded(
            f"{teacher_user.full_name} was already reminded about "
            f"{ctx.subject.name} in {ctx.school_class.name} today.",
        ) from None

    several = only_branch_id_or_several(ctx.tenant) is None
    send_notification(
        event_key=EVENT_SHEET_REMINDER,
        context={
            "teacher_name": teacher_user.full_name,
            "subject_name": ctx.subject.name,
            "class_name": ctx.school_class.name,
            "term_name": ctx.term.name,
            "branch_name": ctx.branch.name if several else "",
            "tenant_name": ctx.tenant.name,
        },
        recipients=[teacher_user],
        tenant=ctx.tenant,
        branch=ctx.branch,
    )
    audit.record(
        tenant=ctx.tenant, user=sender, branch=ctx.branch,
        entity_type="ScoreSheet", entity_id=sheet.pk, entity_label=label(ctx),
        action_type=AuditActionType.UPDATE,
        summary=f"{sender.full_name} reminded {teacher_user.full_name} about {label(ctx)}.",
        class_id=ctx.school_class.pk, subject_id=ctx.subject.pk, term_id=ctx.term.pk,
        reminded_user_id=teacher_user.pk,
    )
    return reminder
