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

from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler

from .constants import LEAVE_DOCUMENT_TYPE, LEAVE_TEMPLATE_CODE, LeaveStatus


@register_handler(LEAVE_DOCUMENT_TYPE)
class LeaveRequestWorkflowHandler(BaseWorkflowHandler):
    document_type = LEAVE_DOCUMENT_TYPE

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
