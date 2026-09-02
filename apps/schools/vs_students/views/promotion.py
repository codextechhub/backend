"""The end-of-session move: preview, run, and the summary afterwards."""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_CLASS_ASSIGN, PERM_MANAGE, PromotionOutcome
from ..models import StudentPromotionBatch
from ..serializers import PromotionBatchSerializer, PromotionRunSerializer
from ..services import promotion as promotion_service
from .base import StudentsViewMixin


def _session(tenant, pk, *, label):
    from schools.vs_academics.models import AcademicSession

    row = AcademicSession.objects.filter(tenant=tenant, pk=pk).first()
    if row is None:
        raise NotFound(f"No such {label} at this school.")
    return row


def _plan_payload(plan):
    counts = plan.counts()
    return {
        "counts": {
            "promote": counts[PromotionOutcome.PROMOTE],
            "repeat": counts[PromotionOutcome.REPEAT],
            "graduate": counts[PromotionOutcome.GRADUATE],
            "hold": counts[PromotionOutcome.HOLD],
            "candidates": len(plan.candidates),
            "excluded": len(plan.student_exceptions),
        },
        "level_map": plan.level_map,
        # Class-wide causes collapse to one entry however many students they
        # cover; per-student causes get one each. A list that repeated a
        # class-wide cause per student would bury the rows that need a decision
        # under the rows that do not.
        "exceptions": {
            "by_class": plan.class_exceptions,
            "by_student": plan.student_exceptions,
        },
        "students": [
            {
                "id": c.student.pk,
                "name": c.student.full_name,
                "student_number": c.student.student_number,
                "from_class": c.enrolment.school_class.name,
                "from_class_id": c.enrolment.school_class_id,
                "to_class": c.target_class.name if c.target_class else None,
                "outcome": c.outcome,
            }
            for c in plan.candidates
        ],
    }


class _PromotionBase(StudentsViewMixin, APIView):
    def _sessions(self, data):
        to_session = _session(self.tenant, data["to_session"], label="target session")
        from_id = data.get("from_session")
        from_session = (
            _session(self.tenant, from_id, label="source session") if from_id
            else self.active_session
        )
        if from_session.pk == to_session.pk:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({
                "to_session": "Pick a different year to promote into.",
            })
        return from_session, to_session

    def _payload(self, request):
        writer = PromotionRunSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        return writer.validated_data


class PromotionPreviewView(_PromotionBase):
    """POST /v1/students/promotions/preview/

    Writes nothing, and runs the *same* classification the run does. A preview
    computed by different code is not a preview, it is a second opinion, and
    the two drift the first time either is fixed.

    docstring-name: Preview a promotion
    """

    def get_permissions(self):
        self.rbac_permission = PERM_MANAGE
        return super().get_permissions()

    def post(self, request):
        data = self._payload(request)
        from_session, to_session = self._sessions(data)
        plan = promotion_service.classify(
            self.tenant, request.user,
            from_session=from_session, to_session=to_session,
            overrides=data.get("overrides"), branch=self.branch_filter,
        )
        return success_response(data={
            "from_session": str(from_session), "to_session": str(to_session),
            **_plan_payload(plan),
        })


class PromotionRunView(_PromotionBase):
    """POST /v1/students/promotions/

    docstring-name: Run a promotion
    """

    def get_permissions(self):
        self.rbac_permission = PERM_MANAGE
        return super().get_permissions()

    def post(self, request):
        # Two keys: the run writes placements, and placing is M13's power.
        self.assert_holds(PERM_MANAGE, PERM_CLASS_ASSIGN)

        data = self._payload(request)
        from_session, to_session = self._sessions(data)
        batch, _ = promotion_service.run(
            self.tenant, request.user,
            from_session=from_session, to_session=to_session,
            overrides=data.get("overrides"), branch=self.branch_filter,
        )
        return success_response(
            f"{batch.promoted} promoted, {batch.graduated} graduated, "
            f"{batch.repeated} repeating and {batch.held} held.",
            data=PromotionBatchSerializer(batch).data, status=201,
        )


class PromotionBatchView(StudentsViewMixin, APIView):
    """GET /v1/students/promotions/<id>/

    docstring-name: One promotion run
    """

    def get_permissions(self):
        self.rbac_permission = PERM_MANAGE
        return super().get_permissions()

    def get(self, request, pk):
        batch = StudentPromotionBatch.objects.filter(
            tenant=self.tenant, pk=pk,
        ).select_related("from_session", "to_session").first()
        if batch is None:
            raise NotFound("No such promotion run at this school.")
        return success_response(data=PromotionBatchSerializer(batch).data)
