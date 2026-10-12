"""Corrections to a mark after its day has closed.

``POST /v1/attendance/corrections/`` files a request, ``GET`` lists them, and
``POST .../<id>/approve/`` and ``.../reject/`` decide one. The list is open to
two kinds of reader: a holder of ``attendance.correction.approve`` reads every
correction in the branches they can see, and a holder of
``attendance.correction.create`` alone reads only the ones they asked for.
Filing needs the create key, and which class may be filed for is decided in
:mod:`..services.access`. A correction of another school answers 404.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.evaluator import has_permission
from vs_rbac.scoping import only_branch_id_or_several

from ..constants import PERM_CORRECTION_APPROVE, PERM_CORRECTION_CREATE, CorrectionStatus
from ..serializers import (
    CorrectionCreateSerializer,
    CorrectionDecisionSerializer,
    CorrectionSerializer,
)
from ..services import corrections
from .base import AttendanceViewMixin

#: What ``?status=`` may ask for, and what each selects.
STATUS_FILTERS = ("pending", "decided")


class _CorrectionView(AttendanceViewMixin, APIView):
    def several_branches(self) -> bool:
        return only_branch_id_or_several(self.tenant) is None

    def render(self, rows) -> list:
        several = self.several_branches()
        return CorrectionSerializer(
            [
                corrections.correction_row(row, several_branches=several, viewer=self.request.user)
                for row in rows
            ],
            many=True, context={"request": self.request},
        ).data

    def render_one(self, correction):
        reloaded = corrections.corrections_visible_to(
            self.request.user, self.tenant, only_own=False,
        ).get(pk=correction.pk)
        return self.render([reloaded])[0]


class CorrectionListCreateView(_CorrectionView):
    """GET, POST /v1/attendance/corrections/

    ``GET`` takes ``?status=pending|decided`` and ``?branch=``, newest first:
    waiting corrections by when they were asked for, decided ones by when they
    were decided.

    docstring-name: Corrections
    """

    @property
    def rbac_permission(self):
        if self.request.method == "POST":
            return PERM_CORRECTION_CREATE
        return [PERM_CORRECTION_APPROVE, PERM_CORRECTION_CREATE]

    def get(self, request):
        wanted = request.query_params.get("status")
        if wanted and wanted not in STATUS_FILTERS:
            raise ValidationError({"status": "Ask for pending or decided corrections."})
        decides = has_permission(request.user, PERM_CORRECTION_APPROVE, tenant=self.tenant)
        queryset = corrections.corrections_visible_to(
            request.user, self.tenant, only_own=not decides,
        )
        branch = self.branch_filter()
        if branch is not None:
            queryset = queryset.filter(mark__register__branch=branch)
        if wanted == "pending":
            queryset = queryset.filter(status=CorrectionStatus.PENDING)
        elif wanted == "decided":
            queryset = queryset.exclude(status=CorrectionStatus.PENDING).order_by(
                "-decided_at", "-id",
            )
        return self.paginated(queryset, self.render)

    def post(self, request):
        serializer = CorrectionCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        body = serializer.validated_data
        ctx = self.load_context_for(body["register_class_id"], body["date"])
        correction = corrections.request_correction(
            ctx, request.user, body["student_id"],
            to_mark=body["to_mark"],
            reason_code_id=body.get("reason_code_id"),
            note=body.get("note", ""),
            arrived_at=body.get("arrived_at"),
        )
        return success_response(
            "Correction requested.", self.render_one(correction), status=201,
        )


class _DecisionView(_CorrectionView):
    rbac_permission = PERM_CORRECTION_APPROVE
    approve = True
    done = ""

    def post(self, request, correction_id):
        serializer = CorrectionDecisionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        correction = corrections.decide(
            self.tenant, request.user, correction_id, approve=self.approve,
            decision_note=serializer.validated_data.get("decision_note", ""),
        )
        return success_response(self.done, self.render_one(correction))


class CorrectionApproveView(_DecisionView):
    """POST /v1/attendance/corrections/<id>/approve/

    docstring-name: Approve a correction
    """

    approve = True
    done = "Correction approved."


class CorrectionRejectView(_DecisionView):
    """POST /v1/attendance/corrections/<id>/reject/

    docstring-name: Reject a correction
    """

    approve = False
    done = "Correction rejected."
