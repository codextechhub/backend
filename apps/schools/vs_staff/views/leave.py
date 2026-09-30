"""Filing, correcting and cancelling leave.

**No approval lives here.** A request is submitted to the workflow engine on
creation and decided through the engine's own instance actions under its own
keys, so no single key both files an absence and allows it. An administrator
recording leave on somebody's behalf still submits it, which is what stops a
school having two kinds of leave with two different meanings.

Three keys, because three different people do three different things.
``school.leave.apply`` is for your own and every member of staff holds it.
``school.leave.update`` and ``school.leave.cancel`` are for somebody else's.
``school.leave.view`` is for
reading a colleague's, and a teacher does not hold it: who is off sick is not
something every colleague may read. The school's profile policy may still show
a person's leave to their line managers, and by default shows a person their
own (``services/visibility.py``).

Closed before go-live: there is nothing to record or approve before anybody has
started.

FRD M12 v2.1, FR-013.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from core.response import success_response

from ..constants import (
    PERM_LEAVE_APPLY,
    PERM_LEAVE_CANCEL,
    PERM_LEAVE_UPDATE,
    PERM_LEAVE_VIEW,
)
from ..models import LeaveRequest
from ..serializers import LeaveSerializer, LeaveUpdateSerializer, LeaveWriteSerializer
from ..services import leave as leave_service
from ..services.scoping import is_self
from ..services.visibility import GROUP_LEAVE
from .base import StaffViewMixin


class StaffLeaveView(StaffViewMixin, APIView):
    """GET/POST /v1/i/me/staff/<id>/leave/

    ``?as_at=YYYY-MM-DD`` answers as at the end of that day (``as_at.py``).

    GET carries the person's requests, ``days_taken`` across every session,
    and ``balances`` for one session: every leave type's ``allowance``,
    ``taken``, ``pending`` and ``remaining`` (``services.leave.balances``).
    The session is the one covering today (or the as-at day), falling back to
    the school's active session, or the one named by ``?session=<id>``, which
    must be this school's. ``balance_session`` names it, and is null with
    ``balances`` empty where the school has no session to count against.

    docstring-name: A staff member's leave
    """

    profile_group = GROUP_LEAVE

    def get_permissions(self):
        """A read is admitted in the handler; filing needs a key.

        Applying for your own needs ``school.leave.apply``, which every member
        of staff holds. Filing somebody else's needs ``school.leave.update``.
        """
        from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

        if self._is_read():
            return super().get_permissions()
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def _is_own(self) -> bool:
        from ..models import StaffProfile

        user = getattr(self.request, "user", None)
        tenant = getattr(self.request, "tenant", None)
        if not getattr(user, "pk", None) or tenant is None:
            return False
        return StaffProfile.objects.filter(
            tenant=tenant, pk=self.kwargs.get("pk"), user_id=user.pk,
        ).exists()

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return PERM_LEAVE_VIEW
        return PERM_LEAVE_APPLY if self._is_own() else PERM_LEAVE_UPDATE

    def get(self, request, pk):
        from vs_history.as_at import parse_as_at

        from .. import as_at as past

        staff, _access, admission = self.admit_profile_read(pk)
        as_at = parse_as_at(request)
        self.refuse_as_at_unless_full(as_at, admission)
        session = self._balance_session(staff, as_at.date if as_at else None)
        if as_at is None:
            rows = list(staff.leave_requests.select_related("requested_by", "staff__user"))
            days_taken = leave_service.days_taken(staff)
            counted = None
        else:
            record, children, _meta = past.staff_at(staff, as_at)
            rows = sorted(children["leave_requests"], key=lambda row: row.start_date, reverse=True)
            for row in rows:
                row.staff = record
            days_taken = past.days_taken_at(rows)
            counted = rows
        read_context = self._request_context(rows, as_at)
        return success_response(data={
            "leave": LeaveSerializer(rows, many=True, context={"as_at": as_at, **read_context}).data,
            "days_taken": days_taken,
            "balances": (
                leave_service.balances(staff, session, rows=counted)
                if session is not None else []
            ),
            "balance_session": (
                {
                    "id": session.pk, "name": session.name,
                    "start_date": session.start_date, "end_date": session.end_date,
                }
                if session is not None else None
            ),
            "balance_note": (
                "Allowances are per academic session, set in Settings, Staff. "
                "Taken counts approved leave and pending counts leave waiting "
                "for a decision; remaining is the allowance less both. A type "
                "with no allowance has no limit."
            ),
        })

    def _request_context(self, rows, as_at):
        """Batch current approval holders and the last correction or cancellation."""
        from django.db.models import F, Q

        from vs_audit.models import AuditActionType, AuditEvent
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStageAction, WorkflowStageApprover,
        )

        ids = [row.pk for row in rows]
        approvals = {}
        changes = {}
        if not ids:
            return {"leave_approvals": approvals, "leave_changes": changes}

        change_rows = (
            AuditEvent.objects.filter(
                tenant=self.tenant, entity_type="LeaveRequest",
                entity_id__in=[str(pk) for pk in ids],
            ).filter(
                Q(action_type=AuditActionType.UPDATE)
                | Q(action_type=AuditActionType.STAFF_LEAVE_DECIDED,
                    metadata__status="CANCELLED")
            ).select_related("actor_user")
        )
        if as_at is not None:
            change_rows = change_rows.filter(event_at__lt=as_at.moment)
        for event in change_rows.order_by("entity_id", "-event_at").distinct("entity_id"):
            changes[int(event.entity_id)] = event

        if as_at is None:
            pending_ids = [str(row.pk) for row in rows if row.status == "PENDING"]
            instances = (
                WorkflowInstance.all_objects.filter(
                    tenant=self.tenant, document_type="schools.leave_request",
                    document_object_id__in=pending_ids, status="IN_PROGRESS",
                ).select_related("current_stage").order_by("-created_at")
            )
            for instance in instances:
                approvals.setdefault(int(instance.document_object_id), {
                    "instance_id": instance.pk,
                    "stage": instance.current_stage.label if instance.current_stage else None,
                    "pending_with": [],
                })
            instance_ids = [value["instance_id"] for value in approvals.values()]
            snapshots = list(
                WorkflowStageApprover.objects.filter(
                    stage_instance__instance_id__in=instance_ids,
                    stage_instance__status="ACTIVE",
                    attempt=F("stage_instance__attempt"),
                ).select_related("user", "stage_instance__instance")
            )
            stage_ids = {snap.stage_instance_id for snap in snapshots}
            acted = set(WorkflowStageAction.objects.filter(
                stage_instance_id__in=stage_ids, reversed_at__isnull=True,
                is_reversal_of__isnull=True,
            ).values_list("stage_instance_id", "actor_id"))
            for snap in snapshots:
                if (snap.stage_instance_id, snap.user_id) in acted:
                    continue
                name = " ".join(part for part in (
                    snap.user.first_name, snap.user.last_name,
                ) if part).strip()
                names = approvals[int(snap.stage_instance.instance.document_object_id)]["pending_with"]
                if name and name not in names:
                    names.append(name)
        return {"leave_approvals": approvals, "leave_changes": changes}

    def _balance_session(self, staff, on_date):
        """The session the balances count: named, covering the day, or active."""
        from schools.vs_academics.models import AcademicSession
        from vs_config.clock import branch_today

        raw = (self.request.query_params.get("session") or "").strip()
        if raw:
            session = (
                AcademicSession.all_objects.filter(tenant=self.tenant, pk=raw).first()
                if raw.isdigit() else None
            )
            if session is None:
                raise NotFound("No such session at this school.")
            return session
        day = on_date or branch_today(self.tenant, staff.branch_id)
        return leave_service.leave_session(staff, day) or self.active_session

    def post(self, request, pk):
        staff = self.get_staff_for_write(pk)
        payload = LeaveWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        row, warnings = leave_service.file_request(
            staff=staff, leave_type=data["leave_type"],
            start_date=data["start_date"], end_date=data["end_date"],
            days=data.get("days"), note=data.get("note", ""),
            actor=request.user, request=request,
        )
        return success_response(
            message="Leave request filed and sent for approval.",
            data={"leave": LeaveSerializer(row).data, "warnings": warnings},
            status=status.HTTP_201_CREATED,
        )


class LeaveDetailView(StaffViewMixin, APIView):
    """PATCH/DELETE /v1/i/me/staff/leave/<id>/

    PATCH corrects a request that has not been decided. DELETE cancels one and
    never removes it: leave taken is part of the employment history, and a
    school asked two years later why somebody was away needs the record rather
    than its absence.

    docstring-name: One leave request
    """

    @property
    def rbac_permission(self):
        return PERM_LEAVE_CANCEL if self.request.method == "DELETE" else PERM_LEAVE_UPDATE

    def _row(self, pk):
        row = (
            LeaveRequest.objects.filter(tenant=self.tenant, pk=pk)
            .select_related("staff__user", "staff__branch", "requested_by")
            .first()
        )
        if row is None:
            raise NotFound("No such leave request at this school.")
        self.get_staff_for_write(row.staff_id)
        return row

    def patch(self, request, pk):
        row = self._row(pk)
        payload = LeaveUpdateSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        row, warnings = leave_service.correct(
            row, actor=request.user, **payload.validated_data,
        )
        return success_response(
            message="Leave request updated.",
            data={"leave": LeaveSerializer(row).data, "warnings": warnings},
        )

    def delete(self, request, pk):
        row = self._row(pk)
        leave_service.cancel(row, actor=request.user)
        return success_response(
            message="Leave request cancelled.", data=LeaveSerializer(row).data,
        )
