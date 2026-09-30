"""Filing, correcting and cancelling an absence.

A leave request is applied for and decided, and this module owns only the first
half. **Nothing here approves anything**: the row is submitted to the workflow
engine on creation and its status is written by the handler's callbacks, so no
single key both files an absence and allows it. An administrator recording leave
on somebody's behalf still submits it for approval, which is what stops a school
having two kinds of leave with two different meanings.

**The leave year is the academic session.** A request belongs to the session of
the school that contains its start date (:func:`leave_session`), so allowances
reset when a new session starts. A school sets an allowance per leave type per
session in Settings, Staff; a type with none has no limit, which is where every
type starts. A balance is then that allowance, what was approved, what is
waiting, and what remains once both are counted (:func:`balances`).

**Filing past an allowance is allowed.** The approver decides: a request carries
``over_allowance_by``, the days it goes past the allowance counting what was
already approved or waiting, worked out when it is filed and again when it is
corrected, and shown to the approver on the approval card.

**Days are working days.** A request counts the school's working weekdays in its
range (Monday to Friday unless the school says otherwise) and, unless the school
says otherwise, leaves out the days its calendar closes the school at the
person's branch or school-wide (:func:`count_days`). A count is worked out when
a request is filed or its dates are corrected; a count already stored is never
recomputed, and a caller of the API may send its own.

FRD M12 v2.1, FR-013.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_config.clock import branch_day_q
from vs_config.display import format_date_range

from ..constants import LEAVE_LIVE_STATUSES, LeaveStatus, LeaveType
from ..exceptions import InvalidDateRange, LeaveAlreadyDecided, NoWorkingDays
from . import audit


def closure_dates(staff, start_date, end_date) -> set:
    """The days in the range the school calendar closes the school for *staff*.

    Closures at the person's main posting and school-wide ones. An event aimed
    at particular classes or levels (Primary 4's speech day) closes the day for
    those pupils, not for the staff, so only an event with no audience counts.
    """
    from django.db.models import Q

    from schools.vs_calendar.models import CalendarEvent
    from schools.vs_calendar.services.calendar import non_teaching_dates

    reach = Q(branch__isnull=True)
    if staff.branch_id:
        reach |= Q(branch_id=staff.branch_id)
    events = CalendarEvent.all_objects.filter(
        reach, tenant_id=staff.tenant_id, closes_school=True,
        start_date__lte=end_date, end_date__gte=start_date, audience__isnull=True,
    ).distinct()
    return non_teaching_dates(events)


def count_days(staff, start_date, end_date, *, rules=None) -> int:
    """The school's working days in the range, less its closures.

    Working weekdays and whether closures are left out are the school's
    (``services.rules.leave_rules``); Monday to Friday, closures left out,
    where it has not chosen.
    """
    from datetime import timedelta

    from .rules import leave_rules

    rules = rules or leave_rules(staff.tenant)
    closed = (
        closure_dates(staff, start_date, end_date) if rules.exclude_closures else set()
    )
    total, day = 0, start_date
    while day <= end_date:
        if day.isoweekday() in rules.working_days and day not in closed:
            total += 1
        day += timedelta(days=1)
    return total


def _counted(staff, start_date, end_date, days, rules):
    """*days* where the caller sent it, else the school's count, never zero."""
    if days is not None:
        return days
    counted = count_days(staff, start_date, end_date, rules=rules)
    if counted == 0:
        raise NoWorkingDays(field="start_date")
    return counted


def leave_session(staff, on_date):
    """The academic session a leave starting *on_date* belongs to, or None.

    The session covering the date that applies to the person's main posting,
    preferring one in progress or finished over a draft; for somebody posted
    school-wide, a school-wide session first. None where no session covers the
    date, and then no allowance applies.
    """
    from schools.vs_academics.models import AcademicSession, SessionStatus

    candidates = list(
        AcademicSession.all_objects.filter(
            tenant_id=staff.tenant_id, start_date__lte=on_date, end_date__gte=on_date,
        ).prefetch_related("branch_links"),
    )

    def fit(session):
        branch_ids = {row.branch_id for row in session.branch_links.all()}
        applies = not branch_ids or staff.branch_id in branch_ids
        return (
            not applies,
            session.status == SessionStatus.DRAFT,
            session.is_school_wide == bool(staff.branch_id),
            -session.start_date.toordinal(),
        )

    applicable = sorted(candidates, key=fit)
    return applicable[0] if applicable else None


def _session_rows(staff, session, *, exclude_pk=None):
    from ..models import LeaveRequest

    rows = LeaveRequest.objects.filter(
        tenant_id=staff.tenant_id, staff=staff,
        status__in=tuple(LEAVE_LIVE_STATUSES),
        start_date__gte=session.start_date, start_date__lte=session.end_date,
    )
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    return rows


def over_allowance(staff, leave_type, start_date, days, *, exclude_pk=None, rules=None) -> int:
    """Days past *leave_type*'s allowance this leave would take its session to.

    Counts what is already approved or waiting in the same session, so two
    requests that each fit alone but not together are both seen. 0 where the
    type has no allowance or no session covers the start date.
    """
    from django.db.models import Sum

    from .rules import leave_rules

    rules = rules or leave_rules(staff.tenant)
    allowance = rules.allowance_for(leave_type)
    if allowance is None:
        return 0
    session = leave_session(staff, start_date)
    if session is None:
        return 0
    used = (
        _session_rows(staff, session, exclude_pk=exclude_pk)
        .filter(leave_type=leave_type).aggregate(total=Sum("days"))["total"] or 0
    )
    return max(0, used + days - allowance)


def balances(staff, session, *, rows=None, rules=None) -> list[dict]:
    """Every leave type's allowance, taken, pending and remaining for *session*.

    ``taken`` sums APPROVED requests and ``pending`` PENDING ones, among the
    requests starting inside the session. ``remaining`` is the allowance less
    both, so it says what could still be filed without going over; it goes
    negative once the approver lets a request past the allowance, and it is
    null for a type with no allowance. *rows* are the requests to count where
    the caller already holds them (a record read as at an earlier day).
    """
    from .rules import leave_rules

    rules = rules or leave_rules(staff.tenant)
    if rows is None:
        rows = _session_rows(staff, session)
    taken = dict.fromkeys(LeaveType.values, 0)
    pending = dict.fromkeys(LeaveType.values, 0)
    for row in rows:
        if not session.start_date <= row.start_date <= session.end_date:
            continue
        if row.status == LeaveStatus.APPROVED:
            taken[row.leave_type] = taken.get(row.leave_type, 0) + row.days
        elif row.status == LeaveStatus.PENDING:
            pending[row.leave_type] = pending.get(row.leave_type, 0) + row.days
    out = []
    for code, label in LeaveType.choices:
        allowance = rules.allowance_for(code)
        out.append({
            "leave_type": code,
            "label": label,
            "allowance": allowance,
            "taken": taken[code],
            "pending": pending[code],
            "remaining": (
                None if allowance is None else allowance - taken[code] - pending[code]
            ),
        })
    return out


def _over_warning(leave, rules):
    """The filing's own warning where it goes past the allowance, or None."""
    if not leave.over_allowance_by:
        return None
    allowance = rules.allowance_for(leave.leave_type)
    over = leave.over_allowance_by
    return {
        "code": "OVER_ALLOWANCE",
        "message": (
            f"This goes {over} day{'s' if over != 1 else ''} past the "
            f"{allowance} day{'s' if allowance != 1 else ''} of "
            f"{leave.get_leave_type_display().lower()} leave allowed this "
            f"session. It is filed, and the approver decides."
        ),
        "over_allowance_by": over,
    }


def _overlap_warning(clashes, tenant):
    """The warning naming each clashing request, or None when nothing clashes.

    Each clash is its type and its span, the span in the school's own date
    format ("Annual 27 - 31 Oct 2025", or "Annual 27/10/2025 - 31/10/2025").
    """
    if not clashes:
        return None
    return {
        "code": "LEAVE_OVERLAP",
        "message": (
            "This overlaps leave already recorded for this person: "
            + ", ".join(
                f"{row.get_leave_type_display()} "
                f"{format_date_range(row.start_date, row.end_date, tenant)}"
                for row in clashes
            )
        ),
        "leave_ids": [row.pk for row in clashes],
    }


def overlapping(staff, start_date, end_date, *, exclude_pk=None):
    """This person's live requests that clash with these dates.

    Only PENDING and APPROVED count. A rejected or cancelled request is not an
    absence and warning about it would send somebody looking for a clash that
    does not exist.
    """
    from ..models import LeaveRequest

    queryset = LeaveRequest.objects.filter(
        tenant_id=staff.tenant_id, staff=staff,
        status__in=tuple(LEAVE_LIVE_STATUSES),
        start_date__lte=end_date, end_date__gte=start_date,
    )
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return list(queryset.order_by("start_date"))


@transaction.atomic
def file_request(*, staff, leave_type, start_date, end_date, days=None, note="",
                 actor, request=None):
    """Create a request and submit it for approval, in one transaction.

    Returns ``(leave, warnings)``. Overlapping leave **warns and does not
    refuse**: a school recording a sick day inside a booked annual leave is
    correcting a record, not making a mistake, and a refusal would send them to
    cancel and re-enter. Leave past its allowance warns too (``OVER_ALLOWANCE``)
    and is filed with ``over_allowance_by`` for the approver.

    ``days`` left out is counted by :func:`count_days`; dates holding no day
    the school counts are refused (``NO_WORKING_DAYS``).
    """
    from vs_workflow.services.submission import submit_for_approval

    from ..models import LeaveRequest

    if end_date < start_date:
        raise InvalidDateRange(
            "Leave cannot end before it starts.", field="end_date",
        )

    from .rules import leave_rules

    rules = leave_rules(staff.tenant)
    days = _counted(staff, start_date, end_date, days, rules)
    clashes = overlapping(staff, start_date, end_date)
    leave = LeaveRequest.objects.create(
        tenant=staff.tenant, staff=staff, leave_type=leave_type,
        start_date=start_date, end_date=end_date, days=days,
        over_allowance_by=over_allowance(
            staff, leave_type, start_date, days, rules=rules,
        ),
        note=note or "", status=LeaveStatus.PENDING, requested_by=actor,
    )
    submit_for_approval(leave, actor)
    audit.emit_leave_recorded(leave, actor=actor)

    warnings = [
        w for w in (
            _over_warning(leave, rules), _overlap_warning(clashes, staff.tenant),
        ) if w
    ]
    return leave, warnings


@transaction.atomic
def correct(leave, *, leave_type=None, start_date=None, end_date=None, days=None,
            note=None, actor=None):
    """Change a request that has not been decided yet.

    Refused once it is approved or rejected, because a request whose dates
    change after approval is a different request, and editing it in place would
    leave an approval attached to something nobody approved.

    New dates with no ``days`` are counted again by :func:`count_days`, and
    ``over_allowance_by`` is worked out again for whatever the request now
    says.
    """
    if leave.status != LeaveStatus.PENDING:
        raise LeaveAlreadyDecided(
            "This leave request has already been decided, so it cannot be "
            "changed. Cancel it and file a new one.",
            status=leave.status,
        )

    if leave_type is not None:
        leave.leave_type = leave_type
    if start_date is not None:
        leave.start_date = start_date
    if end_date is not None:
        leave.end_date = end_date
    if leave.end_date < leave.start_date:
        raise InvalidDateRange(
            "Leave cannot end before it starts.", field="end_date",
        )
    from .rules import leave_rules

    rules = leave_rules(leave.tenant)
    if days is not None:
        leave.days = days
    elif start_date is not None or end_date is not None:
        leave.days = _counted(leave.staff, leave.start_date, leave.end_date, None, rules)
    if note is not None:
        leave.note = note
    leave.over_allowance_by = over_allowance(
        leave.staff, leave.leave_type, leave.start_date, leave.days,
        exclude_pk=leave.pk, rules=rules,
    )
    leave.save()

    clashes = overlapping(
        leave.staff, leave.start_date, leave.end_date, exclude_pk=leave.pk,
    )
    warnings = [
        w for w in (
            _over_warning(leave, rules), _overlap_warning(clashes, leave.tenant),
        ) if w
    ]
    return leave, warnings


@transaction.atomic
def cancel(leave, *, actor):
    """Withdraw a request. Never a delete.

    Leave taken is part of the employment history, and a school asked two years
    later why somebody was away needs the record rather than its absence. The
    workflow instance is withdrawn with it, so an approver is not left holding a
    decision on something that has been called off.
    """
    from vs_workflow.models import WorkflowInstance
    from vs_workflow.services import actions as workflow_actions

    if leave.status == LeaveStatus.CANCELLED:
        return leave

    instance = (
        WorkflowInstance.all_objects.filter(
            document_type=leave.workflow_document_type,
            document_object_id=str(leave.pk),
        )
        .order_by("-pk")
        .first()
    )
    leave.status = LeaveStatus.CANCELLED
    leave.decided_at = timezone.now()
    leave.save(update_fields=["status", "decided_at", "updated_at"])

    if instance is not None:
        try:
            workflow_actions.cancel(
                instance.id, actor, "The leave request was cancelled.",
            )
        except Exception:
            # The row is cancelled either way. An instance already in a terminal
            # state refuses to be cancelled again, and that must not leave a
            # school holding a request it cannot withdraw.
            pass

    audit.emit_leave_decided(leave, actor=actor)
    return leave


def days_taken(staff, *, since=None, until=None):
    """Days taken per leave type, counted from approved requests.

    Across every session unless *since* or *until* narrows it. The balance for
    one session, against the school's allowances, is :func:`balances`.
    """
    from django.db.models import Sum

    from ..models import LeaveRequest

    queryset = LeaveRequest.objects.filter(
        tenant_id=staff.tenant_id, staff=staff, status=LeaveStatus.APPROVED,
    )
    if since is not None:
        queryset = queryset.filter(end_date__gte=since)
    if until is not None:
        queryset = queryset.filter(start_date__lte=until)
    rows = (
        queryset.values("leave_type")
        .annotate(days=Sum("days"))
        .order_by("leave_type")
    )
    return [{"leave_type": row["leave_type"], "days": row["days"] or 0} for row in rows]


def _covers_today(tenant, today):
    """Leave rows covering *today*, or today where the person on leave works.

    Without *today*, each request is judged on the day at the branch of the
    person it belongs to, so somebody at a Nairobi branch is back from leave at
    Nairobi's midnight, not Lagos's. *tenant* falls back to the request's,
    which is the tenant ``LeaveRequest.objects`` already scopes these queries
    to, so the day and the rows always belong to the same school.
    """
    from django.db.models import Q

    if today is not None:
        return Q(start_date__lte=today, end_date__gte=today)
    from vs_tenants.context import get_current_tenant

    return branch_day_q(
        tenant if tenant is not None else get_current_tenant(), "staff__branch",
        lambda day: Q(start_date__lte=day, end_date__gte=day),
    )


def on_leave_expression(*, today=None, tenant=None):
    """Is this person's leave running, as a queryset expression.

    The same question :func:`on_leave_today` answers, in the one form that
    composes: a set of ids cannot be filtered on, grouped by, or counted, and
    the directory has to do all three. Every surface that decides whether
    somebody reads as On Leave goes through this, so the row, the filter and
    the header count cannot disagree about who is away.

    APPROVED only. A pending request is somebody asking, and a rejected or
    cancelled one is an absence that never happened.
    """
    from django.db.models import Exists, OuterRef

    from ..models import LeaveRequest

    return Exists(
        LeaveRequest.objects.filter(
            _covers_today(tenant, today),
            staff=OuterRef("pk"), status=LeaveStatus.APPROVED,
        ),
    )


def on_leave_until_expression(*, today=None, tenant=None):
    """When the leave that is running today ends, as a queryset expression.

    The companion to :func:`on_leave_expression`, and the answer to the question
    a reader asks the moment they see the chip: until when. Without it a screen
    can say somebody is away and not when they are back, which sends a head
    teacher looking for cover to the Leave tab to find out.

    The LATEST end date where two approved absences overlap today, because that
    is the day they actually return. Two overlapping approvals are unusual and
    are not refused - the filing warns rather than blocking - so the case has to
    resolve to something, and the earlier date would say somebody was back while
    the second absence was still running.
    """
    from django.db.models import OuterRef, Subquery

    from ..models import LeaveRequest

    return Subquery(
        LeaveRequest.objects.filter(
            _covers_today(tenant, today),
            staff=OuterRef("pk"), status=LeaveStatus.APPROVED,
        )
        .order_by("-end_date")
        .values("end_date")[:1],
    )


def on_leave_today(tenant, *, today=None):
    """Staff ids with approved leave covering today.

    The set form, for callers holding people rather than a queryset. It answers
    the same question as :func:`on_leave_expression` and must keep answering it
    the same way: this is what a person's employment status READS as, not a
    warning about it disagreeing with one. Nobody sets On Leave by hand any
    more, so there is no disagreement left to report.
    """
    from ..models import LeaveRequest

    return set(
        LeaveRequest.objects.filter(
            _covers_today(tenant, today),
            tenant=tenant, status=LeaveStatus.APPROVED,
        ).values_list("staff_id", flat=True)
    )
