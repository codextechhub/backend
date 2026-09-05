"""Filing, correcting and cancelling leave.

**No approval lives here.** A request is submitted to the workflow engine on
creation and decided through the engine's own instance actions under its own
keys, so no single key both files an absence and allows it. An administrator
recording leave on somebody's behalf still submits it, which is what stops a
school having two kinds of leave with two different meanings.

Three keys, because three different people do three different things.
``school.leave.apply`` is for your own and every member of staff holds it.
``school.leave.manage`` is for somebody else's. ``school.leave.view`` is for
reading a colleague's, and a teacher does not hold it: who is off sick is not
something every colleague may read.

Closed before go-live: there is nothing to record or approve before anybody has
started.

FRD M12 v2.1, FR-013.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_LEAVE_APPLY, PERM_LEAVE_MANAGE, PERM_LEAVE_VIEW
from ..models import LeaveRequest
from ..serializers import LeaveSerializer, LeaveUpdateSerializer, LeaveWriteSerializer
from ..services import leave as leave_service
from ..services.scoping import is_self
from .base import StaffViewMixin


class StaffLeaveView(StaffViewMixin, APIView):
    """GET/POST /v1/i/me/staff/<id>/leave/

    docstring-name: A staff member's leave
    """

    def get_permissions(self):
        """Your own leave needs no key beyond being signed in.

        Applying for it needs ``school.leave.apply``, which every member of
        staff holds. Reading or filing somebody else's needs the other two.
        """
        from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

        if self.request.method in ("GET", "HEAD", "OPTIONS") and self._is_own():
            return [IsAuthenticatedAndActive()]
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
        return PERM_LEAVE_APPLY if self._is_own() else PERM_LEAVE_MANAGE

    def get(self, request, pk):
        staff = self.get_staff(pk)
        rows = staff.leave_requests.select_related("requested_by", "staff__user")
        return success_response(data={
            "leave": LeaveSerializer(rows, many=True).data,
            "days_taken": leave_service.days_taken(staff),
            # Said explicitly, because a screen that shows days taken beside
            # nothing else will be asked for a balance, and there is none.
            "balance_note": (
                "Days taken, counted from approved requests. There is no "
                "balance: nothing records an entitlement to count against."
            ),
        })

    def post(self, request, pk):
        staff = self.get_staff(pk)
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

    rbac_permission = PERM_LEAVE_MANAGE

    def _row(self, pk):
        row = (
            LeaveRequest.objects.filter(tenant=self.tenant, pk=pk)
            .select_related("staff__user", "staff__branch", "requested_by")
            .first()
        )
        if row is None:
            raise NotFound("No such leave request at this school.")
        self.get_staff(row.staff_id)
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
