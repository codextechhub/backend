"""Reminding a class teacher that a register has not been taken.

An administrator who sees a class missing from the day's registers sends the
class teacher a reminder, in the app and by email. **One reminder per class per
day**, so a row that reads "Reminded" stays reminded and nobody is nagged by two
administrators working the same list.

A reminder goes to the class teacher only, and only when there is a class
teacher who is on the staff roll and able to sign in. A class with none has
nobody to remind, which the refusal says, rather than quietly reminding someone
else.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_notifications.notify import send_notification
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import EVENT_REGISTER_REMINDER
from ..exceptions import AlreadyReminded, AlreadySubmitted, NoClassTeacher
from ..models import RegisterReminder
from .marks import assert_register_is_due
from .registers import RegisterContext


def _reachable_teacher(school_class):
    """The class teacher's account, or None when there is nobody to remind."""
    teacher = school_class.class_teacher
    if teacher is None or not teacher.is_on_roll:
        return None
    user = teacher.user
    return user if user.may_sign_in else None


@transaction.atomic
def remind(ctx: RegisterContext, sender) -> RegisterReminder:
    """Send the class teacher a reminder for this class and day.

    Refused for a day that has not come or a closed day, a register already
    submitted, a class with nobody to remind, and a class already reminded
    that day.
    """
    assert_register_is_due(ctx)
    if ctx.is_submitted:
        raise AlreadySubmitted("This register has already been taken, so there is nothing to remind.")
    teacher_user = _reachable_teacher(ctx.school_class)
    if teacher_user is None:
        raise NoClassTeacher()

    try:
        with transaction.atomic():
            reminder = RegisterReminder.objects.create(
                tenant=ctx.tenant, school_class=ctx.school_class,
                date=ctx.date, sent_by=sender,
            )
    except IntegrityError:
        raise AlreadyReminded(
            f"{teacher_user.full_name} was already reminded about "
            f"{ctx.school_class.name}'s register for this day.",
        ) from None

    several = only_branch_id_or_several(ctx.tenant) is None
    send_notification(
        event_key=EVENT_REGISTER_REMINDER,
        context={
            "teacher_name": teacher_user.full_name,
            "class_name": ctx.school_class.name,
            "date": format_date(ctx.date, ctx.tenant, branch=ctx.branch),
            "is_today": ctx.date == branch_today(ctx.tenant, ctx.branch),
            "branch_name": ctx.branch.name if several else "",
            "school_name": ctx.tenant.name,
        },
        recipients=[teacher_user],
        tenant=ctx.tenant,
        branch=ctx.branch,
    )
    return reminder
