"""The school plan page: what a school is on, and the two ways to change it.

An operator taking a call about a refusal needs three things, and until this
existed the console could give none of them: what the school is on, why each
module sits at the depth it does, and a way to change it that cannot leave the
school half-moved.

Platform-owned, all of it. What a school pays for is a decision between CodeX
and the school, so a school administrator does not change their own plan here
however their roles are composed. Reading it is the same answer: the depths
are commercial facts about the relationship rather than settings a school
tunes, and the school-facing surface for "what can I do" is the effective
capability read, which already exists.

Rebanding is deliberately absent. Moving a feature between Core and Plus
reprices the whole book at once, including schools invoiced last week, so it
stays a code change in ``vs_rbac.permission_bands`` that goes through review
and deploy. What is here is per-school: one customer, one decision, audited.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from core.response import success_response
from vs_config.models import Capability
from vs_config.services.capabilities import clear_depth_grant, set_depth_grant
from vs_rbac.permissions import (
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
)

from ..models import School
from ..serializers import (
    ChangeSchoolPlanSerializer,
    SchoolPlanUpliftSerializer,
)
from ..services.packages import change_plan, plan_overview


class _PlanView(APIView):
    """Every route here is the platform's, whatever the caller's roles say."""

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]

    def get_school(self, slug):
        return get_object_or_404(School.objects.select_related("tenant"), slug=slug)


class SchoolPlanView(_PlanView):
    """GET, PATCH /v1/i/<slug>/plan/

    docstring-name: A school's plan
    """

    def get_permissions(self):
        self.rbac_permission = (
            "platform.schools.view" if self.request.method == "GET"
            else "platform.schools.manage"
        )
        return super().get_permissions()

    def get(self, request, slug):
        school = self.get_school(slug)
        return success_response(
            "School plan retrieved.", data=plan_overview(school),
        )

    def patch(self, request, slug):
        school = self.get_school(slug)
        serializer = ChangeSchoolPlanSerializer(
            data=request.data, context={"school": school},
        )
        serializer.is_valid(raise_exception=True)

        setup, previous, rows = change_plan(
            school=school,
            plan=serializer.validated_data["package_plan"],
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
            expires_at=serializer.validated_data.get("subscription_expires_at"),
        )
        return success_response(
            f"{school.name} moved from {previous.name} to {setup.package_plan.name}, "
            f"and {len(rows)} module grants were re-applied.",
            data=plan_overview(school),
        )


class SchoolPlanUpliftView(_PlanView):
    """POST /v1/i/<slug>/plan/uplifts/

    docstring-name: Give a school deeper reach for a while
    """

    rbac_permission = "platform.schools.manage"

    def post(self, request, slug):
        school = self.get_school(slug)
        serializer = SchoolPlanUpliftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        set_depth_grant(
            capability=serializer.validated_data["capability"],
            tenant=school.tenant,
            depth=serializer.validated_data["depth"],
            actor=request.user,
            starts_at=serializer.validated_data.get("starts_at"),
            ends_at=serializer.validated_data.get("ends_at"),
            reason=serializer.validated_data["reason"],
        )
        return success_response(
            "Uplift recorded. It ends on its own date and returns the school "
            "to the depth its plan pays for.",
            data=plan_overview(school),
        )


class SchoolPlanUpliftDetailView(_PlanView):
    """DELETE /v1/i/<slug>/plan/uplifts/<capability>/

    docstring-name: Withdraw an uplift
    """

    rbac_permission = "platform.schools.manage"

    def delete(self, request, slug, capability):
        school = self.get_school(slug)
        row = get_object_or_404(
            Capability, key=capability, parent__isnull=True, is_active=True,
        )
        cleared = clear_depth_grant(
            capability=row, tenant=school.tenant, actor=request.user,
            reason=request.data.get("reason", "") if request.data else "",
        )
        message = (
            "Uplift withdrawn. The module returns to the depth the plan pays for."
            if cleared else "No uplift was in place for that module."
        )
        return success_response(message, data=plan_overview(school))
