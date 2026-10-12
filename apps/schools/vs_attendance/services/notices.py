"""Telling a child's family about an absence, and about its correction.

**One email per absent child per day, to the primary guardian, and only when
the school has notices on.** Late and Excused send nothing: the school knows
where those children are. A child already noticed that day (they were marked
absent in another class's register, say) is not noticed again.

**Who is written to.** The primary guardian only, never the next guardian down
the record. Bright Star marks Tunde absent; his mother is his primary contact
and the school holds no address for her. His uncle has an address. Writing to
the uncle tells the wrong household a child is in trouble, so Tunde's mark
reads "No email on file" and the school phones his mother as it would have
anyway. A guardian with a login is written to through the account, so they read
it in the app and at the address the account holds; one with only an address is
an unregistered recipient and is mailed.

**The decision and the sending are two steps.** :func:`decide_absence_notices`
runs inside the submit's transaction and settles every mark whose outcome is
already known (notices off, no address, already told that day). What remains is
sent by :func:`send_absence_notices` once the transaction has committed, so a
register that rolled back never emails anybody. A failure to send is logged and
leaves the mark's state at NONE: submitting a register is the act the school
performed, and the email is a consequence of it.

**A same-day change of a mark whose notice was sent** writes a correction notice
to the same guardian (:func:`send_correction_notice`), whether it comes from the
person who took the register or, later, from an approved correction. A change
*to* Absent after submission sends the absence notice the submission would have.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from schools.vs_students.models import StudentGuardian
from vs_config.clock import branch_today
from vs_config.display import format_date
from vs_notifications.notify import UnregisteredRecipient, send_notification
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import (
    EVENT_ABSENCE_NOTICE,
    EVENT_CORRECTION_NOTICE,
    Mark,
    NoticeState,
)
from ..models import StudentMark
from .rules import read_guardian_notices

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reach:
    """How the school can reach one child's primary guardian."""

    guardian: object
    user: object = None
    email: str = ""

    @property
    def has_email(self) -> bool:
        return bool(self.email)


def primary_guardians(tenant, student_ids) -> dict:
    """``{student_id: Reach}`` for each child with a primary guardian.

    One query. A child with no primary guardian is absent from the answer.
    """
    if not student_ids:
        return {}
    links = StudentGuardian.objects.filter(
        tenant=tenant, student_id__in=student_ids, is_primary=True,
    ).select_related("guardian", "guardian__user")
    out = {}
    for link in links:
        guardian = link.guardian
        user = guardian.user if guardian.user_id else None
        email = (user.email if user is not None else "") or guardian.email
        out[link.student_id] = Reach(guardian=guardian, user=user, email=email or "")
    return out


def already_told(tenant, on_date, register_id, student_ids) -> dict:
    """``{student_id: notice_sent_at}`` for children noticed on *on_date* in another register.

    *register_id* is the register being decided, or None for one not yet
    started, and is left out so a register never counts against itself.
    """
    if not student_ids:
        return {}
    rows = (
        StudentMark.objects.filter(
            tenant=tenant, student_id__in=student_ids, register__date=on_date,
            notice_state=NoticeState.SENT,
        )
        .exclude(register_id=register_id)
        .order_by("notice_sent_at")
    )
    out = {}
    for row in rows:
        out.setdefault(row.student_id, row.notice_sent_at)
    return out


def preview(tenant, on_date, register_id, absent_student_ids) -> dict:
    """How many guardians submitting would email, and how many have no address.

    The same decision :func:`decide_absence_notices` takes, made without
    writing it, so the submit step shows the school exactly what it is about
    to do.
    """
    if not read_guardian_notices(tenant):
        return {"notices_on": False, "will_email": 0, "no_email": 0}
    reach = primary_guardians(tenant, absent_student_ids)
    told = already_told(tenant, on_date, register_id, absent_student_ids)
    will, none = 0, 0
    for student_id in absent_student_ids:
        if student_id in told:
            continue
        if reach.get(student_id) is not None and reach[student_id].has_email:
            will += 1
        else:
            none += 1
    return {"notices_on": True, "will_email": will, "no_email": none}


def decide_absence_notices(tenant, register, marks) -> list:
    """Settle every absent mark whose outcome is known now; return the ids still to send.

    Runs in the submit's transaction. *marks* are the register's absent,
    unlocked marks.
    """
    if not marks:
        return []
    now = timezone.now()
    if not read_guardian_notices(tenant):
        for mark in marks:
            mark.notice_state = NoticeState.NOTICES_OFF
            mark.updated_at = now
        StudentMark.all_objects.bulk_update(marks, ["notice_state", "updated_at"])
        return []

    student_ids = [mark.student_id for mark in marks]
    reach = primary_guardians(tenant, student_ids)
    told = already_told(tenant, register.date, register.pk, student_ids)
    to_send = []
    for mark in marks:
        mark.updated_at = now
        if mark.student_id in told:
            mark.notice_state = NoticeState.SENT
            mark.notice_sent_at = told[mark.student_id] or now
        elif mark.student_id not in reach or not reach[mark.student_id].has_email:
            mark.notice_state = NoticeState.NO_EMAIL
        else:
            to_send.append(mark.pk)
    StudentMark.all_objects.bulk_update(
        marks, ["notice_state", "notice_sent_at", "updated_at"],
    )
    return to_send


def notice_context(mark, *, mark_label="") -> dict:
    """What the templates read for one child's notice.

    The branch is named only at a school with several, where it tells a parent
    which site the message came from; at a school with one it repeats what the
    letterhead already says. ``is_today`` lets an absence noticed the same day
    say "today" and a late-submitted register name its date instead.
    """
    register = mark.register
    tenant = register.tenant
    several = only_branch_id_or_several(tenant) is None
    return {
        "student_first_name": mark.student.first_name,
        "student_last_name": mark.student.last_name,
        "class_name": register.school_class.name,
        "date": format_date(register.date, tenant, branch=register.branch),
        "is_today": register.date == branch_today(tenant, register.branch),
        "branch_name": register.branch.name if several else "",
        "school_name": tenant.name,
        "mark_label": mark_label,
    }


def _send(event_key, mark, reach, *, mark_label="") -> bool:
    """Send one notice to *reach*; True when at least one channel carried it."""
    register = mark.register
    if reach.user is not None:
        recipients, unregistered = [reach.user], None
    else:
        recipients = []
        unregistered = [
            UnregisteredRecipient(email=reach.email, name=reach.guardian.full_name),
        ]
    sent = send_notification(
        event_key=event_key,
        context=notice_context(mark, mark_label=mark_label),
        recipients=recipients,
        unregistered_recipients=unregistered,
        tenant=register.tenant,
        branch=register.branch,
    )
    return bool(sent)


def send_absence_notices(tenant, mark_ids) -> int:
    """Email the guardians of the marks in *mark_ids*; returns how many went.

    Called once the submitting transaction has committed. A mark whose channels
    the school has switched off in its notification settings reads "Notices are
    off", because from the guardian's side that is what happened.
    """
    marks = list(
        StudentMark.objects.filter(tenant=tenant, pk__in=mark_ids)
        .select_related("student", "register", "register__school_class", "register__branch")
    )
    reach = primary_guardians(tenant, [m.student_id for m in marks])
    delivered = 0
    for mark in marks:
        try:
            if _send_one_absence(mark, reach.get(mark.student_id)):
                delivered += 1
        except Exception:
            logger.exception(
                "Absence notice for mark %s could not be sent. The register stands.",
                mark.pk,
            )
    return delivered


def _send_one_absence(mark, reach) -> bool:
    if reach is None or not reach.has_email:
        mark.notice_state = NoticeState.NO_EMAIL
        mark.save(update_fields=["notice_state", "updated_at"])
        return False
    if _send(EVENT_ABSENCE_NOTICE, mark, reach):
        mark.notice_state = NoticeState.SENT
        mark.notice_sent_at = timezone.now()
    else:
        mark.notice_state = NoticeState.NOTICES_OFF
    mark.save(update_fields=["notice_state", "notice_sent_at", "updated_at"])
    return mark.notice_state == NoticeState.SENT


def send_correction_notice(mark, new_mark) -> bool:
    """Tell the guardian who was emailed the absence that it was corrected.

    Does nothing unless the mark's absence notice was sent. Never raises: the
    correction is the act and the email is its consequence. Returns whether a
    notice went.
    """
    if mark.notice_state != NoticeState.SENT:
        return False
    try:
        reach = primary_guardians(mark.tenant, [mark.student_id]).get(mark.student_id)
        if reach is None or not reach.has_email:
            return False
        mark = StudentMark.objects.select_related(
            "student", "register", "register__school_class", "register__branch",
        ).get(pk=mark.pk)
        return _send(
            EVENT_CORRECTION_NOTICE, mark, reach,
            mark_label=Mark(new_mark).label,
        )
    except Exception:
        logger.exception(
            "Correction notice for mark %s could not be sent. The change stands.",
            mark.pk,
        )
        return False
