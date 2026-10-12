"""A day's lessons and one lesson's register: list, read, mark, fill in, submit.

``GET /v1/attendance/lessons/`` is the administrator's list of the day's
lessons. Every other route is ``/v1/attendance/lessons/<slot>/<date>/...``. A
lesson is the timetable slot the caller names, on a date that falls on its
weekday; a slot or a student of another school is a 404, as is a class in a
branch the caller cannot see.

The permission key on a route says whether the caller may do this kind of thing
at all. Which lesson they may mark is decided in :mod:`..services.access`,
because "only the lessons I teach" is a fact about the timetable and no key can
express it. A lesson register sends nothing to anyone: none of these routes
notifies a guardian, a teacher or an administrator.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    LESSON_FIELD_RESOURCE,
    PERM_REGISTER_MARK,
    PERM_REGISTER_MARK_ANY,
    PERM_REGISTER_VIEW,
    Mark,
)
from ..exceptions import AdministratorsOnly
from ..models import LessonMark
from ..serializers import LessonListSerializer, LessonMarkWriteSerializer
from ..services import lesson_list, lesson_marks, marks
from ..services.access import assert_may_mark_lesson, may_take_any
from .base import AttendanceViewMixin


class LessonListView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/lessons/

    ``?date=`` defaults to today at the school and ``?branch=`` narrows the list
    to one branch the caller can see. Needs ``attendance.register.mark_any``: a
    teacher's own lessons are at ``today/mine/``.

    docstring-name: Lessons of the day
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request):
        if not may_take_any(request.user, self.tenant):
            raise AdministratorsOnly()
        body = lesson_list.lessons_list(
            self.tenant, request.user, on_date=self.optional_date(),
            branch=self.branch_filter(),
        )
        return success_response(
            data=LessonListSerializer(body, context={"request": request}).data,
        )


class LessonView(AttendanceViewMixin, APIView):
    """GET /v1/attendance/lessons/<slot_id>/<date>/

    docstring-name: Lesson register
    """

    rbac_permission = PERM_REGISTER_VIEW

    def get(self, request, slot_id, on_date):
        ctx = self.lesson_context(slot_id, on_date)
        return success_response(data=self.serialize_lesson(ctx))


class LessonMarkView(AttendanceViewMixin, APIView):
    """PUT /v1/attendance/lessons/<slot_id>/<date>/marks/<student_id>/

    The body is ``mark`` (Present, Late or Absent), ``arrived_at`` for Late and
    an optional ``note``. A note that is not sent is kept as it was.

    docstring-name: Mark a student in a lesson
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def put(self, request, slot_id, on_date, student_id):
        ctx = self.lesson_context(slot_id, on_date)
        # Before the body is looked at, so a caller who may not mark this
        # lesson is told so rather than which of its fields they may write.
        marks.assert_register_is_due(ctx)
        assert_may_mark_lesson(request.user, ctx.tenant, ctx.slot, ctx.school_class)

        existing = None
        if ctx.register is not None:
            existing = LessonMark.objects.filter(
                tenant=ctx.tenant, register=ctx.register, student_id=student_id,
            ).first()
        serializer = LessonMarkWriteSerializer(
            existing, data=request.data, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        fields = serializer.validated_data
        lesson_marks.set_mark(
            ctx, request.user, student_id,
            mark=fields["mark"],
            arrived_at=fields.get("arrived_at"),
            note=fields["note"] if "note" in fields else marks.UNSET,
        )
        return success_response("Mark saved.", self.reread_lesson(ctx))


class LessonFillPresentView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/lessons/<slot_id>/<date>/fill-present/

    Marks every unmarked, unlocked row Present. Submits nothing.

    docstring-name: Everyone else present in a lesson
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def post(self, request, slot_id, on_date):
        ctx = self.lesson_context(slot_id, on_date)
        marks.assert_register_is_due(ctx)
        assert_may_mark_lesson(request.user, ctx.tenant, ctx.slot, ctx.school_class)
        assert_writable(request, LESSON_FIELD_RESOURCE, {"mark": Mark.PRESENT}, creating=True)
        filled = lesson_marks.fill_present(ctx, request.user)
        body = self.reread_lesson(ctx)
        body["filled"] = filled
        return success_response(
            f"{filled} marked Present." if filled else "Nobody was left to mark.", body,
        )


class LessonSubmitView(AttendanceViewMixin, APIView):
    """POST /v1/attendance/lessons/<slot_id>/<date>/submit/

    docstring-name: Submit the lesson register
    """

    rbac_permission = [PERM_REGISTER_MARK, PERM_REGISTER_MARK_ANY]

    def post(self, request, slot_id, on_date):
        ctx = self.lesson_context(slot_id, on_date)
        lesson_marks.submit(ctx, request.user)
        return success_response("Lesson register submitted.", self.reread_lesson(ctx))
