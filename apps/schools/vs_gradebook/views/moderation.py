"""Moderation: the queue of sheets waiting for a department head, and the head's decision.

``GET /v1/gradebook/moderation/`` lists the term's moderated sheets: the head of a
department sees those of the departments they head, and a holder of
``gradebook.hub.view`` sees every moderated sheet in their branches, read-only
unless they also head the department. Approving and returning need
``gradebook.moderation.approve`` and that the caller heads the subject's
department; a sheet that is not submitted and waiting is refused with
``NOT_SUBMITTED``.
"""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.evaluator import has_permission
from vs_rbac.field_enforcement import assert_writable

from ..constants import PERM_HUB_VIEW, PERM_MODERATION_APPROVE, SHEET_FIELD_RESOURCE
from ..serializers import ModerationQueueSerializer, ReturnBodySerializer, SheetSerializer
from ..services import access, moderation, sheets
from .base import GradebookViewMixin


class ModerationQueueView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/moderation/?term=&branch=

    Each sheet row names its class and subject, main teacher, how many of the
    roll are marked, the class average (mean percentage of complete rows, two
    decimals), highest and lowest totals, the pass rate (whole percent), its
    status, the other classes of the programme that have a sheet of the same
    subject (``compare``) and whether the caller can decide on it now
    (``can_decide``).

    docstring-name: Moderation queue
    """

    rbac_permission = [PERM_MODERATION_APPROVE, PERM_HUB_VIEW]

    def get(self, request):
        body = moderation.queue(
            self.tenant, request.user, self.term_from_query(), self.branch_from_query(),
            sees_every_sheet=has_permission(request.user, PERM_HUB_VIEW, tenant=self.tenant),
        )
        return success_response(
            data=dict(ModerationQueueSerializer(body, context={"request": request}).data),
        )


class _DecisionView(GradebookViewMixin, APIView):
    rbac_permission = PERM_MODERATION_APPROVE

    def load(self, class_id, subject_id):
        ctx = sheets.load(
            self.tenant, self.request.user, class_id, subject_id, self.term_from_query(),
        )
        access.assert_heads_department(self.request.user, self.tenant, ctx.subject)
        return ctx

    def answer(self, message, ctx):
        fresh = sheets.load(
            self.tenant, self.request.user, ctx.school_class.pk, ctx.subject.pk, ctx.term,
        )
        return success_response(
            message,
            dict(SheetSerializer(sheets.payload(fresh), context={"request": self.request}).data),
        )


class SheetApproveView(_DecisionView):
    """POST /v1/gradebook/sheets/<class_id>/<subject_id>/approve/?term=

    Approves a submitted, moderated sheet: it is in. Answers with the sheet.

    docstring-name: Approve a score sheet
    """

    def post(self, request, class_id, subject_id):
        ctx = self.load(class_id, subject_id)
        moderation.approve(ctx)
        return self.answer("Sheet approved.", ctx)


class SheetReturnView(_DecisionView):
    """POST /v1/gradebook/sheets/<class_id>/<subject_id>/return/?term=

    Body ``{"note": "..."}``, required (``NOTE_REQUIRED``). Returns a submitted,
    moderated sheet to its main teacher, who is told in the app; the sheet
    reopens with the head's name and note above the grid. A role that may not
    write the return note is refused. Answers with the sheet.

    docstring-name: Return a score sheet
    """

    def post(self, request, class_id, subject_id):
        ctx = self.load(class_id, subject_id)
        assert_writable(request, SHEET_FIELD_RESOURCE, ["note"])
        body = ReturnBodySerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        moderation.return_sheet(ctx, body.validated_data.get("note", ""))
        return self.answer("Sheet returned.", ctx)
