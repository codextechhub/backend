"""Workflow handler for the leave-request document type.

Registered from ``VsStaffConfig.ready()`` so the engine knows what to do when a
leave instance is approved, rejected, withdrawn or cancelled. The engine never
imports this app; this app registers itself with the engine, which is the same
direction ``vs_finance``, ``vs_procurement`` and ``vs_payments`` run in.

**The status column is written here and nowhere else.** No serializer sets it,
because a status a form can set is a status that disagrees with the instance
that decided it: an administrator could mark their own leave approved without
anybody voting, and the profile would show an approval the trail has no record
of.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_workflow.conditions.fields import ConditionField
from vs_workflow.constants import ConditionFieldType, DocumentAudience
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler

from .constants import LEAVE_DOCUMENT_TYPE, LEAVE_TEMPLATE_CODE, LeaveStatus, LeaveType


@register_handler(LEAVE_DOCUMENT_TYPE)
class LeaveRequestWorkflowHandler(BaseWorkflowHandler):
    document_type = LEAVE_DOCUMENT_TYPE
    # Leave is kept on a school's staff records; the platform keeps none.
    audience = DocumentAudience.SCHOOL

    condition_fields = (
        ConditionField("document.leave_type", "Leave type", "document",
                       ConditionFieldType.CHOICE, tuple(LeaveType.choices)),
        ConditionField("document.days", "Days requested", "document",
                       ConditionFieldType.NUMBER),
    )

    def resolve_default_template_code(self, document) -> str:
        return LEAVE_TEMPLATE_CODE

    def get_document_summary(self, document) -> dict:
        """What an approver sees on the card, before they open anything.

        The person's name and the dates, because an approver deciding six
        requests in a morning is deciding between people and days rather than
        between ids. The leave TYPE is here too and is the one field worth
        pausing over: sick leave says something about somebody's health, and it
        appears on this card because an approver cannot decide a request without
        knowing what kind it is. It goes no further than the approval surface,
        which is why ``school.leave.view`` is not granted to teacher.
        """
        staff = getattr(document, "staff", None)
        user = getattr(staff, "user", None)
        name = ""
        if user is not None:
            name = " ".join(
                part for part in (user.first_name, user.last_name) if part
            ).strip()
        days = getattr(document, "days", 0)
        return {
            "title": name or "Leave request",
            "subtitle": f"{document.get_leave_type_display()} leave",
            "fields": [
                {"label": "From", "value": str(document.start_date)},
                {"label": "To", "value": str(document.end_date)},
                {"label": "Days", "value": str(days)},
                {"label": "Job title", "value": getattr(staff, "job_title", "") or "-"},
            ],
        }

    def validate_document(self, document, requested_by) -> None:
        """Only a pending request may be submitted.

        Resubmitting a decided request would attach a second instance to a row
        whose status already came from the first, and the two would disagree
        about what the school allowed.
        """
        from vs_workflow.exceptions import WorkflowError

        if document.status != LeaveStatus.PENDING:
            raise WorkflowError(
                "This leave request has already been decided, so it cannot be "
                "submitted again.",
                error_code="INVALID_DOCUMENT_STATE",
            )

    def _settle(self, instance, status):
        from .models import LeaveRequest
        from .services.audit import emit_leave_decided

        row = LeaveRequest.all_objects.filter(pk=instance.document_object_id).first()
        if row is None:
            return
        # Only a pending row moves. A cancelled request whose instance is
        # decided afterwards must stay cancelled: the person withdrew it, and an
        # approval arriving later does not put them back on leave.
        if row.status != LeaveStatus.PENDING:
            return
        with transaction.atomic():
            row.status = status
            row.decided_at = timezone.now()
            row.save(update_fields=["status", "decided_at", "updated_at"])
            emit_leave_decided(row, actor=getattr(instance, "requested_by", None))

    def on_approved(self, instance, context: dict) -> None:
        self._settle(instance, LeaveStatus.APPROVED)

    def on_rejected(self, instance, context: dict) -> None:
        self._settle(instance, LeaveStatus.REJECTED)

    def on_withdrawn(self, instance, context: dict) -> None:
        self._settle(instance, LeaveStatus.CANCELLED)

    def on_cancelled(self, instance, context: dict) -> None:
        self._settle(instance, LeaveStatus.CANCELLED)

    def reversal_block_reason(self, document) -> str | None:
        """Why this leave approval can no longer be undone, or ``None``.

        Approval releases one thing, and it is not on this row: the person is
        away. Nobody is stored as On Leave, so the directory derives it from an
        approved request covering today, and the school reads that when it
        arranges who takes the class.

        Three situations, two answers. **Leave that has not started** has
        released nothing yet: nobody reads as away, no day has been taken, and
        putting the request back to pending describes exactly where the school
        stands, which is the case a reversal exists for. **Leave that is
        running** is the opposite. The teacher is not in front of the class this
        morning, the directory says so, and withdrawing the approval record
        would make them read as present without bringing them back. **Leave that
        has finished** is the same fact after the event: the days were taken,
        ``days_taken`` counts them from approved rows, and reopening the
        approval describes a school still deciding about an absence it has
        already had.

        So the refusal begins on the first day and does not lift afterwards. An
        absence already under way is stopped by cancelling the request, where
        the person reads as present again and the cancellation is recorded as
        what it is, rather than by editing the approval behind it.

        A request that is pending, rejected or cancelled released nothing
        whatever its dates, and neither did one whose row has since gone.
        """
        if document is None or document.status != LeaveStatus.APPROVED:
            return None
        today = timezone.localdate()
        if document.start_date > today:
            return None
        if document.end_date < today:
            return (
                "This leave has already been taken, so its approval cannot be "
                "undone. Cancel the request instead if the absence never "
                "happened."
            )
        return (
            "This leave is already running and the person reads as On Leave, "
            "so its approval cannot be undone. Cancel the request instead, "
            "which is what puts them back on the directory as present."
        )

    def on_action_reversed(self, instance, context: dict) -> None:
        """Put a decided request back to pending when its vote is reversed.

        The status column is written from the instance and nowhere else, so a
        decision the workflow has withdrawn has to be withdrawn here too, or the
        person's profile keeps showing leave that nobody currently approves.

        A cancelled request stays cancelled: they withdrew it, and reopening the
        approval behind it does not put them back on leave.
        """
        from .models import LeaveRequest

        row = LeaveRequest.all_objects.filter(pk=instance.document_object_id).first()
        if row is None or row.status not in (LeaveStatus.APPROVED, LeaveStatus.REJECTED):
            return
        with transaction.atomic():
            row.status = LeaveStatus.PENDING
            row.decided_at = None
            row.save(update_fields=["status", "decided_at", "updated_at"])
