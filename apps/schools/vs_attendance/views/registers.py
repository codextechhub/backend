"""A class's register for one day: read it, mark it, fill it in, submit it, remind.

Every route is ``/v1/attendance/registers/<class>/<date>/...``. The permission
key on a route says whether the caller may do this kind of thing at all; which
class they may take is decided in :mod:`..services.access`, because "only my
own class" is a fact about the class and no permission key can express it.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from schools.vs_academics.class_teacher_field import may_read_class_teacher
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    FIELD_RESOURCE,
    PERM_REGISTER_MARK,
    PERM_REGISTER_MARK_ANY,
    PERM_REGISTER_REMIND,
    PERM_REGISTER_VIEW,
    Mark,
)
from ..models import StudentMark
from ..serializers import MarkWriteSerializer
from ..services import marks, reminders
from ..services.access import assert_may_take
from .base import AttendanceViewMixin


class RegisterView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/registers/<class_id>/<date>/

    docstring-name: Daily register
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request, class_id, on_date):
        ctx = self.register_context(class_id, on_date)
        return success_response(data=self.serialize(ctx))


class MarkView(AttendanceViewMixin, APIView):
    """PUT /v1/attendance/registers/<class_id>/<date>/marks/<student_id>/

    The body is the whole of the row: ``mark``, and ``arrived_at``,
    ``reason_code`` and ``note`` where the mark carries them. A note that is not
    sent is kept as it was.

    docstring-name: Mark a student
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def put(self, request, class_id, on_date, student_id):
        ctx = self.register_context(class_id, on_date)
        # Before the body is looked at, so a caller who may not take this
        # class is told so rather than which of its fields they may write.
        marks.assert_register_is_due(ctx)
        assert_may_take(request.user, ctx.tenant, ctx.school_class)

        existing = None
        if ctx.register is not None:
            existing = StudentMark.objects.filter(
                tenant=ctx.tenant, register=ctx.register, student_id=student_id,
            ).first()
        serializer = MarkWriteSerializer(
            existing, data=request.data, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        fields = serializer.validated_data
        marks.set_mark(
            ctx, request.user, student_id,
            mark=fields["mark"],
            arrived_at=fields.get("arrived_at"),
            reason_code_id=fields.get("reason_code_id"),
            note=fields["note"] if "note" in fields else marks.UNSET,
        )
        return success_response("Mark saved.", self.reread(ctx))


class FillPresentView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/registers/<class_id>/<date>/fill-present/

    Marks every unmarked, unlocked row Present. Submits nothing.

    docstring-name: Everyone else present
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def post(self, request, class_id, on_date):
        ctx = self.register_context(class_id, on_date)
        marks.assert_register_is_due(ctx)
        assert_may_take(request.user, ctx.tenant, ctx.school_class)
        assert_writable(request, FIELD_RESOURCE, {"mark": Mark.PRESENT}, creating=True)
        filled = marks.fill_present(ctx, request.user)
        body = self.reread(ctx)
        body["filled"] = filled
        return success_response(
            f"{filled} marked Present." if filled else "Nobody was left to mark.", body,
        )


class SubmitView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/registers/<class_id>/<date>/submit/

    docstring-name: Submit the register
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def post(self, request, class_id, on_date):
        ctx = self.register_context(class_id, on_date)
        marks.submit(ctx, request.user)
        return success_response("Register submitted.", self.reread(ctx))


class RemindView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/registers/<class_id>/<date>/remind/

    docstring-name: Remind the class teacher
    """

    rbac_permission = PERM_REGISTER_REMIND

    def post(self, request, class_id, on_date):
        ctx = self.register_context(class_id, on_date)
        reminder = reminders.remind(ctx, request.user)
        teacher = ctx.school_class.class_teacher
        answer = {"reminded": True, "reminded_at": reminder.sent_at}
        if may_read_class_teacher(request):
            answer["class_teacher_name"] = teacher.user.full_name if teacher is not None else None
        return success_response("Reminder sent.", answer, status=201)
