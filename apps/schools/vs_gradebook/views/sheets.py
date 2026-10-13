"""The score sheet: read it, save scores to it, submit it, remind its teacher.

All four routes are ``/v1/gradebook/sheets/<class_id>/<subject_id>/...`` and take
the term as ``?term=<id>`` (the school's current term when it is absent).

The key says whether the caller may use the screen at all; which sheet is a rule
of the sheet (:mod:`..services.access`). A class or subject of another school, a
class in a branch the caller cannot see, a subject the class does not take and a
term of another school all answer 404. Reading and entering are separate rights:
the main teacher enters, and an assistant, the class teacher and the head of the
department read.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    PERM_HUB_VIEW,
    PERM_SHEET_ENTER,
    PERM_SHEET_ENTER_ANY,
    PERM_SHEET_REMIND,
    PERM_SHEET_VIEW,
    SHEET_FIELD_RESOURCE,
)
from ..serializers import ScoresWriteSerializer, SheetSerializer, SubmitResultSerializer
from ..services import reminders, resolve, sheets
from .base import GradebookViewMixin


class _SheetView(GradebookViewMixin, APIView):
    def load(self, class_id, subject_id, *, rule=sheets.READ):
        return sheets.load(
            self.tenant, self.request.user, class_id, subject_id, self.term_from_query(),
            rule=rule,
        )

    def render(self, ctx) -> dict:
        """The sheet as the caller may read it."""
        return dict(SheetSerializer(
            sheets.payload(ctx), context={"request": self.request},
        ).data)

    def reread(self, ctx) -> dict:
        """The sheet re-read after a write, so it shows what was just saved."""
        return self.render(self.load(ctx.school_class.pk, ctx.subject.pk))


class SheetView(_SheetView):
    """GET /v1/gradebook/sheets/<class_id>/<subject_id>/

    One subject's sheet for one class and term: the header (who teaches it, the
    examination paper, where it stands, what the caller may do), the columns, a
    line for every student on the sheet's roll and the class figures. A sheet
    nobody has started reads NOT_STARTED. An optional subject lists only the
    students who take it. Needs ``gradebook.sheet.view`` (or ``hub.view``); the
    caller must also have a part in the sheet or read every sheet in their
    branches.

    docstring-name: Score sheet
    """

    rbac_permission = [PERM_SHEET_VIEW, PERM_HUB_VIEW]

    def get(self, request, class_id, subject_id):
        return success_response(data=self.render(self.load(class_id, subject_id)))


class SheetScoresView(_SheetView):
    """PUT /v1/gradebook/sheets/<class_id>/<subject_id>/scores/

    Body ``{"cells": [{"student": id, "component": id, "mark": int|null,
    "marker": "ABS"|"EXC"|null}]}``. A mark and a marker are exclusive; both null
    blanks the cell. All or nothing: when any cell is refused the answer is a 422
    whose ``error.detail.cells`` lists each one as ``{student, component, code,
    message}`` and nothing is saved. Answers with the whole sheet. Refused with
    ``SHEET_LOCKED`` once submitted and ``CLASS_LOCKED`` once the class has gone
    to the principal. Only the main teacher (or an ``enter_any`` holder) may
    save, and a role that may not write marks is refused the request.

    docstring-name: Save scores
    """

    rbac_permission = [PERM_SHEET_ENTER, PERM_SHEET_ENTER_ANY]

    def put(self, request, class_id, subject_id):
        ctx = self.load(class_id, subject_id, rule=sheets.ENTER)
        assert_writable(request, SHEET_FIELD_RESOURCE, ["cells"])
        body = ScoresWriteSerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        sheets.save_scores(ctx, body.validated_data["cells"])
        return success_response("Scores saved.", self.reread(ctx))


class SheetSubmitView(_SheetView):
    """POST /v1/gradebook/sheets/<class_id>/<subject_id>/submit/

    Submits the sheet. Refused with ``BLANKS_REMAIN`` (and the count in
    ``error.detail.blank_count``) while any cell is blank. Answers
    ``{"next": "MODERATION" | "CLASS_TEACHER", "sheet": ...}``: moderation when
    the class's programme is moderated and the subject's department has a head.

    docstring-name: Submit a score sheet
    """

    rbac_permission = [PERM_SHEET_ENTER, PERM_SHEET_ENTER_ANY]

    def post(self, request, class_id, subject_id):
        ctx = self.load(class_id, subject_id, rule=sheets.ENTER)
        where = sheets.submit(ctx)
        body = {"next": where, "sheet": sheets.payload(self.load(class_id, subject_id))}
        return success_response(
            "Sheet submitted.",
            dict(SubmitResultSerializer(body, context={"request": request}).data),
        )


class SheetRemindView(_SheetView):
    """POST /v1/gradebook/sheets/<class_id>/<subject_id>/remind/

    Sends the sheet's main teacher a reminder in the app and by email, once per
    sheet per school day. Refused when the sheet is already in (``SHEET_LOCKED``),
    nobody is the main teacher (``NO_MAIN_TEACHER``), the teacher has left or
    cannot sign in (``TEACHER_UNREACHABLE``, saying which) or the teacher was
    already reminded today (``ALREADY_REMINDED``). Needs
    ``gradebook.sheet.remind``.

    docstring-name: Remind a sheet's teacher
    """

    rbac_permission = PERM_SHEET_REMIND

    def post(self, request, class_id, subject_id):
        ctx = self.load(class_id, subject_id, rule=sheets.ANY)
        reminders.remind(ctx, request.user)
        teacher = resolve.main_teacher(
            ctx.school_class, ctx.subject, ctx.school_class.session_id,
        )
        return success_response(
            f"{teacher.user.full_name} has been reminded.",
            {"reminded_today": True, "main_teacher": {"id": teacher.pk, "name": teacher.user.full_name}},
        )
