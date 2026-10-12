"""The two Today screens.

``GET /v1/attendance/today/`` is the school-wide hub for administrators and
``GET /v1/attendance/today/mine/`` is a teacher's own registers. Both need
``attendance.register.view``. The hub additionally needs a role that works the
whole school (``attendance.register.mark_any`` or
``attendance.correction.approve``): a teacher holding neither is told where
their own screen is rather than shown every class. What the hub's lists
contain is settled by branch visibility, not by the key.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.evaluator import has_permission

from ..constants import (
    PERM_CORRECTION_APPROVE,
    PERM_REGISTER_MARK_ANY,
    PERM_REGISTER_VIEW,
)
from ..exceptions import AdministratorsOnly
from ..serializers import HubSerializer, TeacherHubSerializer
from ..services import hub
from .base import AttendanceViewMixin


class TodayView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/today/

    ``?date=`` defaults to today at the school and ``?branch=`` narrows the hub
    to one branch the caller can see.

    docstring-name: Today, the administrator's hub
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request):
        user, tenant = request.user, self.tenant
        decides = has_permission(user, PERM_CORRECTION_APPROVE, tenant=tenant)
        if not (decides or has_permission(user, PERM_REGISTER_MARK_ANY, tenant=tenant)):
            raise AdministratorsOnly()
        body = hub.admin_hub(
            tenant, user, on_date=self.optional_date(), branch=self.branch_filter(),
            can_decide=decides,
        )
        return success_response(data=HubSerializer(body, context={"request": request}).data)


class TodayMineView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/today/mine/

    docstring-name: Today, my registers
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request):
        body = hub.teacher_hub(self.tenant, request.user, on_date=self.optional_date())
        return success_response(
            data=TeacherHubSerializer(body, context={"request": request}).data,
        )
