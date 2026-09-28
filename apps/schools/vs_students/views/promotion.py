"""The end-of-session move: its rules, preview, run, and the summary afterwards."""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from ..constants import (
    PERM_CLASS_ASSIGN,
    PERM_PROMOTE,
    PERM_SETTINGS_UPDATE,
    PERM_VIEW,
    PromotionOutcome,
    StudentStatus,
)
from ..models import StudentPromotionBatch
from ..serializers import (
    PromotionBatchSerializer,
    PromotionRulesSerializer,
    PromotionRunSerializer,
)
from ..services import promotion as promotion_service
from ..services.scoping import scope_to_visible_branches
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
        # The school's promotion rules as stored. capacity_mode below is the
        # effective one, which is what the run applies.
        "rules": plan.rules.stored(),
        "level_map": plan.level_map,
        # Target classes the run would fill past capacity. Under WARN the run
        # refuses them until allow_over_capacity is sent; under HARD it refuses
        # them outright; under OFF the list is always empty.
        "over_capacity": plan.over_capacity,
        "capacity_mode": plan.capacity_mode,
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
                "suspended": c.student.status == StudentStatus.SUSPENDED,
            }
            for c in plan.candidates
        ],
    }


class PromotionRulesView(StudentsViewMixin, APIView):
    """GET, PUT /v1/students/promotion-rules/

    The school's own promotion rules: what the end-of-year promotion does with
    suspended pupils and with pupils confirmed but not placed, whether arms
    move up whole or are spread across next year's classes, and which capacity
    rule the run follows. Reading needs ``school.students.view``, because the
    promotion screen renders from it; changing needs
    ``school.settings.update``, because these are the school's settings
    rather than a student record, and a caller whose reach is the whole
    school, because the rules bind every branch. A branch-bound caller holding
    the key is refused with a 403 (SHARED_RECORD_READ_ONLY) and nothing is
    written.

    ``capacity_mode`` is the school's choice, which may be FOLLOW_ENROLMENT;
    ``effective_capacity_mode`` is what a run applies, and
    ``enrolment_capacity_mode`` the enrolment rule it follows.

    PUT takes all four rules every time, plus an optional ``reason`` for the
    audit trail, and answers with the GET body. Refusals are 400s keyed on the
    field, in sentences.

    docstring-name: Promotion rules
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def get(self, request):
        from ..services.promotion_rules import read_promotion_rules

        return success_response(data=read_promotion_rules(self.tenant).as_dict())

    def put(self, request):
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.promotion_rules import write_promotion_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message=(
                "Only a school-wide administrator can change the school's "
                "promotion rules."
            ),
        )
        writer = PromotionRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        rules = write_promotion_rules(
            self.tenant, request.user, reason=data.pop("reason", ""), **data,
        )
        return success_response("Promotion rules saved.", data=rules.as_dict())


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
        self.rbac_permission = PERM_PROMOTE
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
        self.rbac_permission = PERM_PROMOTE
        return super().get_permissions()

    def post(self, request):
        # Two keys: the run writes placements, and placing is vs_academics'.
        # Promotion and transfer are sold at different depths.
        self.assert_holds(PERM_PROMOTE, PERM_CLASS_ASSIGN)

        data = self._payload(request)
        from_session, to_session = self._sessions(data)
        batch, _ = promotion_service.run(
            self.tenant, request.user,
            from_session=from_session, to_session=to_session,
            overrides=data.get("overrides"), branch=self.branch_filter,
            allow_over_capacity=data.get("allow_over_capacity", False),
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
        self.rbac_permission = PERM_PROMOTE
        return super().get_permissions()

    def get(self, request, pk):
        # Inclusive, unlike the student reads in this module: a run with no
        # branch rolled the whole school forward, so it moved this caller's
        # children too and is theirs to read. A run pinned to one site is that
        # site's, and its record of who repeated and who was held back is not
        # another site's to open by naming its id.
        batch = scope_to_visible_branches(
            StudentPromotionBatch.objects.filter(tenant=self.tenant, pk=pk),
            request.user, self.tenant,
        ).select_related("from_session", "to_session").first()
        if batch is None:
            raise NotFound("No such promotion run at this school.")
        return success_response(data=PromotionBatchSerializer(batch).data)
