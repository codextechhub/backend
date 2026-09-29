"""Workflow handlers for the leave-request and new-hire document types.

Registered from ``VsStaffConfig.ready()`` so the engine knows what to do when a
leave or hire instance is approved, rejected, withdrawn or cancelled. The engine never
imports this app; this app registers itself with the engine, which is the same
direction ``vs_finance``, ``vs_procurement`` and ``vs_payments`` run in.

**A leave request's status column is written here and nowhere else.** No serializer sets it,
because a status a form can set is a status that disagrees with the instance
that decided it: an administrator could mark their own leave approved without
anybody voting, and the profile would show an approval the trail has no record
of.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_config.clock import branch_today
from vs_workflow.conditions.fields import ConditionField
from vs_workflow.constants import ConditionFieldType, DocumentAudience
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import register_handler
from vs_workflow.presentation import document_details, fields_section

from .constants import (
    HIRE_DOCUMENT_TYPE,
    HIRE_TEMPLATE_CODE,
    LEAVE_DOCUMENT_TYPE,
    LEAVE_TEMPLATE_CODE,
    EmploymentStatus,
    LeaveStatus,
    LeaveType,
)


def _days(count) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


@register_handler(LEAVE_DOCUMENT_TYPE)
class LeaveRequestWorkflowHandler(BaseWorkflowHandler):
    noun = "Leave request"
    document_type = LEAVE_DOCUMENT_TYPE
    # Leave is kept on a school's staff records; the platform keeps none.
    audience = DocumentAudience.SCHOOL

    condition_fields = (
        ConditionField("document.leave_type", "Leave type", "document",
                       ConditionFieldType.CHOICE, tuple(LeaveType.choices)),
        ConditionField("document.days", "Days requested", "document",
                       ConditionFieldType.NUMBER),
        ConditionField("document.over_allowance_by", "Days over the allowance",
                       "document", ConditionFieldType.NUMBER),
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
        fields = [
            {
                "label": "Dates",
                "value": f"{document.start_date} to {document.end_date}",
            },
        ]
        over = getattr(document, "over_allowance_by", 0) or 0
        if over:
            fields.append({"label": "Over allowance", "value": _days(over)})
        return {
            "title": name or "Leave request",
            "subtitle": f"{document.get_leave_type_display()} leave",
            "fields": fields,
        }

    def get_document_details(self, document) -> dict:
        """The count, the job and the reason, and how far past the allowance it goes.

        The allowance line appears only where the request exceeds it. Filing
        over the allowance is allowed, so this is where the approver learns it.
        """
        staff = getattr(document, "staff", None)
        rows = [
            ("Days", getattr(document, "days", 0)),
            ("Job title", getattr(staff, "job_title", "") or "-"),
            ("Reason", getattr(document, "note", "")),
        ]
        over = getattr(document, "over_allowance_by", 0) or 0
        if over:
            rows.append(("Over allowance by", _days(over)))
        return document_details(fields_section("Leave details", rows))

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
        today = branch_today(document.tenant, document.staff.branch_id)
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


@register_handler(HIRE_DOCUMENT_TYPE)
class StaffHireWorkflowHandler(BaseWorkflowHandler):
    """A new member of staff, held back from their invitation until approved.

    The document is the staff record. Approval sends the invitation; a
    rejection, a withdrawal or a cancellation closes the hire. Both are carried
    out by ``services.hire``, which does nothing to a record no longer awaiting
    approval, so a late or repeated callback changes nothing.

    The person who added somebody may approve the hire only when nobody else
    is on the stage, the rule a role grant keeps: a school with one
    administrator is not left with hires nobody can release, and a school with
    two gets the other person's decision.
    """

    noun = "New staff member"
    document_type = HIRE_DOCUMENT_TYPE
    audience = DocumentAudience.SCHOOL
    allows_requester_self_approval = True
    self_approval_only_when_alone = True

    def resolve_default_template_code(self, document) -> str:
        return HIRE_TEMPLATE_CODE

    def get_document_summary(self, document) -> dict:
        """The person, the job and where they will work."""
        user = getattr(document, "user", None)
        name = ""
        if user is not None:
            name = " ".join(
                part for part in (user.first_name, user.last_name) if part
            ).strip()
        branch = getattr(document, "branch", None)
        return {
            "title": name or "New member of staff",
            "subtitle": document.job_title or "New member of staff",
            "fields": [
                {"label": "Posting", "value": branch.name if branch else "School-wide"},
                {"label": "Starting role", "value": _starting_role_name(document)},
            ],
        }

    def get_document_details(self, document) -> dict:
        user = getattr(document, "user", None)
        return document_details(fields_section("New staff details", [
            ("Email", getattr(user, "email", "") or "-"),
            ("Staff ID", document.staff_number or "-"),
            ("Employment type", document.get_employment_type_display() or "-"),
            ("Hire date", str(document.hire_date) if document.hire_date else "-"),
        ]))

    def validate_document(self, document, requested_by) -> None:
        """Only a record awaiting approval may be submitted, once."""
        from vs_workflow.exceptions import WorkflowError

        if document.employment_status != EmploymentStatus.PENDING_APPROVAL:
            raise WorkflowError(
                "This person is not waiting for approval, so there is nothing to "
                "submit.",
                error_code="INVALID_DOCUMENT_STATE",
            )

    def _profile(self, instance):
        from .models import StaffProfile

        return (
            StaffProfile.all_objects.select_related("user", "tenant")
            .filter(pk=instance.document_object_id).first()
        )

    def _actor(self, context):
        from vs_user.models import User

        actor_id = (context or {}).get("actor_id")
        return User.objects.filter(pk=actor_id).first() if actor_id else None

    def on_approved(self, instance, context: dict) -> None:
        from .services import hire

        profile = self._profile(instance)
        if profile is not None:
            hire.approve(profile, requested_by=instance.requested_by)

    def _close(self, instance, context, reason):
        from .services import hire

        profile = self._profile(instance)
        if profile is not None:
            hire.close(profile, reason=reason, actor=self._actor(context))

    def on_rejected(self, instance, context: dict) -> None:
        self._close(instance, context, "Hire not approved")

    def on_withdrawn(self, instance, context: dict) -> None:
        self._close(instance, context, "Hire withdrawn before approval")

    def on_cancelled(self, instance, context: dict) -> None:
        reason = ((context or {}).get("reason") or "").strip()
        self._close(instance, context, reason or "Hire cancelled before approval")

    def reversal_block_reason(self, document) -> str | None:
        """Why a decided hire cannot be reopened, or ``None`` while it waits.

        An approval sends an invitation the person may already have opened, and
        a rejection closes the account for good. Neither comes back with the
        engine's record of the vote: an approved hire is withdrawn by revoking
        the invitation, and a closed one is added again.
        """
        if document is None:
            return None
        status = document.employment_status
        if status == EmploymentStatus.PENDING_APPROVAL:
            return None
        if status == EmploymentStatus.TERMINATED and document.user.status == "REJECTED":
            return (
                "This hire has already been closed and its account cannot be "
                "reopened, so the decision cannot be undone. Add the person again "
                "instead."
            )
        return (
            "This person has already been sent their invitation, so the approval "
            "cannot be undone. Revoke the invitation instead."
        )


def _starting_role_name(profile) -> str:
    """The role the hire was given at creation, as the school names it."""
    grant = (
        profile.user.tenant_role_assignments.filter(assignment_status="ACTIVE")
        .select_related("role").order_by("pk").first()
    )
    return grant.role.name if grant is not None else "-"
