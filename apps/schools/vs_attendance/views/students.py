"""A student's Attendance tab.

``GET /v1/attendance/students/<student_id>/?term=<term_id>`` needs
``attendance.register.view``. Which students a caller may open is the student
module's rule, applied by the student module's own lookup: another school's
student, and a student in a branch the caller cannot see, answer 404.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_REGISTER_VIEW
from ..serializers import StudentAttendanceSerializer
from ..services.student_tab import student_tab
from .base import AttendanceViewMixin


class StudentAttendanceView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/students/<student_id>/

    docstring-name: Student attendance
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request, student_id):
        raw = request.query_params.get("term")
        if raw in (None, ""):
            term_id = None
        elif str(raw).isdigit():
            term_id = int(raw)
        else:
            raise ValidationError({"term": "Choose a term from the list."})
        body = student_tab(self.tenant, request.user, student_id, term_id)
        return success_response(
            data=StudentAttendanceSerializer(body, context={"request": request}).data,
        )
