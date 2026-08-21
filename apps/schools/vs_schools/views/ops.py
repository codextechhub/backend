from __future__ import annotations

from rest_framework import generics

from core.response import success_response, error_response

from ..models import School
from vs_rbac.permissions import (
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsVisionSuperAdmin,
)
from ..serializers import (
    SchoolDetailSerializer,
    SchoolResetConfigSerializer,
    SchoolServiceStateSerializer,
)


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class _SchoolOpBaseView(ActorContextMixin, generics.GenericAPIView):
    """Base for school operation views."""
    queryset = School.objects.all()
    lookup_field = "slug"

    def _run(self, request, serializer_class):
        school = self.get_object()
        serializer = serializer_class(
            data=request.data,
            context={**self.get_serializer_context(), "school": school},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        school.refresh_from_db()
        return success_response(
            message="School operation completed successfully.",
            data=SchoolDetailSerializer(school, context=self.get_serializer_context()).data,
        )


class SchoolResetConfigView(_SchoolOpBaseView):
    """docstring-name: Reset school configuration"""
    permission_classes = [IsVisionSuperAdmin]

    def post(self, request, *args, **kwargs):
        return self._run(request, SchoolResetConfigSerializer)


class SchoolServiceStateView(_SchoolOpBaseView):
    """POST /i/<slug>/service-state/ - take a school out of service, or back.

    The action ``LastBranchCannotLeaveService`` has been pointing at since it
    was written: it refuses to take a school's only branch out of service and
    tells the operator to deactivate the school instead, which nothing could
    do until now.

    ``IsVisionStaff`` sits beside the key for the same reason the branch
    transition view carries it. ``platform.schools.manage`` is seeded
    restricted/SENSITIVE, but a key's namespace is not its audience: nothing
    stops that key being attached to a role inside a school tenant, and a
    school must never be able to switch itself - or anybody else - off.

    What this does NOT touch is as deliberate as what it does. Branches keep
    their own statuses, so returning a school to service restores exactly the
    arrangement it had, including which branch was main.

    docstring-name: Change school service state
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "platform.schools.manage"

    def post(self, request, *args, **kwargs):
        return self._run(request, SchoolServiceStateSerializer)
