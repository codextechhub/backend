"""Filing, correcting and cancelling an absence.

A leave request is applied for and decided, and this module owns only the first
half. **Nothing here approves anything**: the row is submitted to the workflow
engine on creation and its status is written by the handler's callbacks, so no
single key both files an absence and allows it. An administrator recording leave
on somebody's behalf still submits it for approval, which is what stops a school
having two kinds of leave with two different meanings.

There is no balance, and there will not be one until somebody says where an
entitlement comes from. A balance is an entitlement minus what has been taken,
nothing anywhere records an entitlement, and Nigerian statutory leave is a floor
rather than a schedule that schools vary by grade and by length of service. What
this module reports is days taken, which is a count of approved rows and is
therefore reliable.

FRD M12 v2.1, FR-013.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..constants import LEAVE_LIVE_STATUSES, LeaveStatus
from ..exceptions import InvalidDateRange, LeaveAlreadyDecided
from . import audit


def default_days(start_date, end_date) -> int:
    """The inclusive calendar span, which the school may correct.

    Working days are not calendar days, and nothing in this repository records
    which days a school teaches. A school running Saturday classes and one that
    does not would get different answers from any formula, so the service
    supplies the obvious number and lets somebody who knows change it.
    """
    return (end_date - start_date).days + 1


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
    cancel and re-enter.
    """
    from vs_workflow.services.submission import submit_for_approval

    from ..models import LeaveRequest

    if end_date < start_date:
        raise InvalidDateRange(
            "Leave cannot end before it starts.", field="end_date",
        )

    clashes = overlapping(staff, start_date, end_date)
    leave = LeaveRequest.objects.create(
        tenant=staff.tenant, staff=staff, leave_type=leave_type,
        start_date=start_date, end_date=end_date,
        days=days if days is not None else default_days(start_date, end_date),
        note=note or "", status=LeaveStatus.PENDING, requested_by=actor,
    )
    submit_for_approval(leave, actor)
    audit.emit_leave_recorded(leave, actor=actor)

    warnings = []
    if clashes:
        warnings.append({
            "code": "LEAVE_OVERLAP",
            "message": (
                "This overlaps leave already recorded for this person: "
                + ", ".join(
                    f"{row.get_leave_type_display()} {row.start_date} to {row.end_date}"
                    for row in clashes
                )
            ),
            "leave_ids": [row.pk for row in clashes],
        })
    return leave, warnings


@transaction.atomic
def correct(leave, *, leave_type=None, start_date=None, end_date=None, days=None,
            note=None, actor=None):
    """Change a request that has not been decided yet.

    Refused once it is approved or rejected, because a request whose dates
    change after approval is a different request, and editing it in place would
    leave an approval attached to something nobody approved.
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
    if days is not None:
        leave.days = days
    elif start_date is not None or end_date is not None:
        leave.days = default_days(leave.start_date, leave.end_date)
    if note is not None:
        leave.note = note
    leave.save()

    warnings = []
    clashes = overlapping(
        leave.staff, leave.start_date, leave.end_date, exclude_pk=leave.pk,
    )
    if clashes:
        warnings.append({
            "code": "LEAVE_OVERLAP",
            "message": (
                "This overlaps leave already recorded for this person: "
                + ", ".join(
                    f"{row.get_leave_type_display()} {row.start_date} to {row.end_date}"
                    for row in clashes
                )
            ),
            "leave_ids": [row.pk for row in clashes],
        })
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

    Not a balance and never rendered as one. A count of rows is a fact; a
    balance needs an entitlement, and nothing records one.
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


def on_leave_expression(*, today=None):
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

    today = today or timezone.localdate()
    return Exists(
        LeaveRequest.objects.filter(
            staff=OuterRef("pk"), status=LeaveStatus.APPROVED,
            start_date__lte=today, end_date__gte=today,
        ),
    )


def on_leave_until_expression(*, today=None):
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

    today = today or timezone.localdate()
    return Subquery(
        LeaveRequest.objects.filter(
            staff=OuterRef("pk"), status=LeaveStatus.APPROVED,
            start_date__lte=today, end_date__gte=today,
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

    today = today or timezone.localdate()
    return set(
        LeaveRequest.objects.filter(
            tenant=tenant, status=LeaveStatus.APPROVED,
            start_date__lte=today, end_date__gte=today,
        ).values_list("staff_id", flat=True)
    )
