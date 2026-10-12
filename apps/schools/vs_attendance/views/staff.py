"""The staff register: a day's registers, mark a person, fill in the rest, submit.

``GET /v1/attendance/staff-registers/<date>/`` lists the day's staff registers
in the caller's branches. Every write is
``/v1/attendance/staff-registers/<branch>/<date>/...``: a branch of another
school, or one the caller cannot see, is a 404, as is a person who is not on that
branch's register that day.

The permission key on a route says whether the caller may do this kind of thing
at all: ``attendance.staff.view`` reads and ``attendance.staff.mark`` writes.
There is no "my own" rule, because no member of staff takes their own register.
A staff register sends nothing to anyone: none of these routes notifies a
guardian, a colleague or an administrator.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import assert_writable

from ..constants import PERM_STAFF_MARK, PERM_STAFF_VIEW, STAFF_FIELD_RESOURCE, Mark
from ..models import StaffMark
from ..serializers import StaffMarkWriteSerializer, StaffRegisterSerializer
from ..services import staff_marks, staff_register
from .base import AttendanceViewMixin


class StaffRegistersView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/staff-registers/<date>/

    ``?branch=`` narrows the day to one branch the caller can see. A whole-school
    reader at a school with several branches also receives the people who have no
    home branch, locked, in ``no_home_branch``.

    docstring-name: Staff registers
    """

    rbac_permission = PERM_STAFF_VIEW

    def get(self, request, on_date):
        body = staff_register.read_staff_registers(
            self.tenant, request.user, on_date=self.parse_date(on_date),
            branch=self.branch_filter(),
        )
        return success_response(
            data=StaffRegisterSerializer(body, context={"request": request}).data,
        )


class StaffMarkView(AttendanceViewMixin, APIView):
    """PUT /v1/attendance/staff-registers/<branch_id>/<date>/marks/<staff_id>/

    The body is ``mark`` (Present, Late or Absent), ``arrived_at`` for Late and an
    optional ``note``. A note that is not sent is kept as it was.

    docstring-name: Mark a member of staff
    """

    rbac_permission = PERM_STAFF_MARK

    def put(self, request, branch_id, on_date, staff_id):
        ctx = self.staff_context(branch_id, on_date)
        # Before the body is looked at, so a caller is told the person cannot be
        # marked rather than which of the body's fields they may write.
        staff_register.assert_register_is_due(ctx)
        staff_register.writable_entry(ctx, staff_id)

        existing = StaffMark.objects.filter(
            tenant=ctx.tenant, staff_id=staff_id, date=ctx.date,
        ).first()
        serializer = StaffMarkWriteSerializer(
            existing, data=request.data, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        fields = serializer.validated_data
        staff_marks.set_mark(
            ctx, request.user, staff_id,
            mark=fields["mark"],
            arrived_at=fields.get("arrived_at"),
            note=fields["note"] if "note" in fields else staff_marks.UNSET,
        )
        return success_response("Mark saved.", self.reread_staff(ctx))


class StaffFillPresentView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/staff-registers/<branch_id>/<date>/fill-present/

    Marks every unmarked, unlocked row Present. Submits nothing.

    docstring-name: Everyone else present on the staff register
    """

    rbac_permission = PERM_STAFF_MARK

    def post(self, request, branch_id, on_date):
        ctx = self.staff_context(branch_id, on_date)
        staff_register.assert_register_is_due(ctx)
        assert_writable(request, STAFF_FIELD_RESOURCE, {"mark": Mark.PRESENT}, creating=True)
        filled = staff_marks.fill_present(ctx, request.user)
        body = self.reread_staff(ctx)
        body["filled"] = filled
        return success_response(
            f"{filled} marked Present." if filled else "Nobody was left to mark.", body,
        )


class StaffSubmitView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/staff-registers/<branch_id>/<date>/submit/

    docstring-name: Submit the staff register
    """

    rbac_permission = PERM_STAFF_MARK

    def post(self, request, branch_id, on_date):
        ctx = self.staff_context(branch_id, on_date)
        staff_marks.submit(ctx, request.user)
        return success_response("Staff register submitted.", self.reread_staff(ctx))
