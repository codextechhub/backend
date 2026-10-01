"""Returning a pupil whose suspension has run its course.

A suspension given an end date returns the pupil on that day without anybody
acting. That is the whole point of recording the date: a school that suspends
Tunde for three days has already decided he is back on Thursday, and a
register that still marks him suspended on Thursday is wrong about a child who
is sitting in class. A suspension with no end date is never touched here; it
stands until a person lifts it.

**The day is the pupil's branch's day, never the server's.** Brightfield keeps
Lagos time and its Mombasa branch keeps Nairobi's, and the platform runs in
UTC. At 23:30 UTC on Wednesday it is already Thursday at both of them, so a
sweep judging by the server's day would leave both pupils suspended through
their own Thursday morning; at 00:30 UTC it is still Wednesday in Lagos, so
the same sweep would return the Lekki pupil a day early. Each candidate row is
therefore judged on ``branch_today`` for the branch the pupil attends, and the
query is bounded by the widest day any zone on earth can be showing.

**The return is dated the day it was due, not the day the job ran.** A worker
down from Thursday to Sunday must still record Tunde as back on Thursday, or
the record says he was suspended for six days when the school suspended him
for three, and his attendance, his fees and his file all inherit the lie.

**Only a pupil whose current status is still SUSPENDED is returned.** A pupil
suspended on Monday and withdrawn on Tuesday has left the roll, and the
suspension row with Thursday on it is still sitting in their history. Reading
that row without checking where the pupil is now would put a withdrawn child
back on the roll, release nothing, bill them again and tell nobody. The guard
is a filter on the pupil's own status, not a judgement about the row.

**Nothing is sent.** A suspension being lifted writes to no family, by hand or
by this sweep: there is no event for it, and a school that suspended a pupil
until Thursday told the family so when it suspended them.

Safe to run twice, and safe to miss a day. A second run finds the pupil
already on the roll and the state machine refuses a move to the status they
already hold, so the first thing it does is filter those pupils out. One
pupil's failure is logged and the sweep goes on: a bad row may not cost every
other school its returns.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from vs_config.clock import branch_today

from ..constants import ALLOWED_TRANSITIONS, ON_ROLL, StudentStatus
from ..models import StudentStatusLog

logger = logging.getLogger(__name__)

#: No zone on earth is more than a day away from UTC, so a row whose end date
#: is past tomorrow in UTC cannot be due at any school. This is what keeps the
#: sweep's queryset bounded on a platform with a decade of suspensions in it.
_HORIZON = dt.timedelta(days=1)

#: What a pupil may be returned to: on the roll, and a move the state machine
#: allows from a suspension. Derived rather than written as ACTIVE, so a school
#: that gains a new on-roll status does not quietly gain a sweep that returns
#: pupils to somewhere the transition table forbids.
RETURNABLE_TO = ALLOWED_TRANSITIONS[StudentStatus.SUSPENDED] & ON_ROLL


def due_suspensions():
    """Each currently suspended pupil's live suspension row, where it is due.

    One query for every school on the platform. The rows are narrowed in the
    database to suspensions that carry an end date, that fall inside the widest
    day any zone can be showing, and that belong to a pupil who is *still*
    suspended; the branch's own day is then applied in Python, because the
    answer differs per branch and the row count here is small.

    A pupil suspended, returned and suspended again has several rows with end
    dates. Only the most recent one is their live suspension, so the rows
    arrive newest first per pupil and the rest are dropped.
    """
    rows = (
        StudentStatusLog.all_objects.filter(
            to_status=StudentStatus.SUSPENDED,
            return_date__isnull=False,
            return_date__lte=timezone.now().date() + _HORIZON,
            student__status=StudentStatus.SUSPENDED,
        )
        .select_related("student", "student__tenant", "student__branch")
        .order_by("student_id", "-changed_at", "-id")
    )

    tenants, days, seen, due = {}, {}, set(), []
    for row in rows:
        if row.student_id in seen:
            continue
        seen.add(row.student_id)
        student = row.student
        # One Tenant instance per school, so its zones are read once: the
        # clock memoises them on the instance it is handed, and select_related
        # hands out a fresh one per row.
        tenant = tenants.setdefault(student.tenant_id, student.tenant)
        student.tenant = tenant
        key = (student.tenant_id, student.branch_id)
        if key not in days:
            days[key] = branch_today(tenant, student.branch_id)
        if row.return_date <= days[key]:
            due.append(row)
    return due


def return_suspended_students():
    """Return every pupil whose suspension has ended, and say what happened.

    ``{"returned": n, "skipped": n, "failed": n}``. A skipped pupil is one
    whose suspension began somewhere the state machine cannot put them back,
    which is a data fault rather than a school's decision, so it is logged by
    name and left alone for a person to look at.
    """
    from .status import transition

    returned = skipped = failed = 0
    for row in due_suspensions():
        student = row.student
        if row.from_status not in RETURNABLE_TO:
            skipped += 1
            logger.warning(
                "Student %s was not returned from suspension: the suspension "
                "records them as %s beforehand, which is not a status they "
                "can be put back to. Their suspension ended on %s.",
                student.pk, row.from_status or "nothing", row.return_date,
            )
            continue
        try:
            with transaction.atomic():
                transition(
                    student, row.from_status, actor=None,
                    reason="The suspension ended on the day it was set to end.",
                    effective_date=row.return_date, system=True,
                )
        except Exception:
            failed += 1
            logger.exception(
                "Student %s could not be returned from the suspension that "
                "ended on %s.", student.pk, row.return_date,
            )
            continue
        returned += 1
    return {"returned": returned, "skipped": skipped, "failed": failed}
