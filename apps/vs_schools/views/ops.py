from __future__ import annotations

from rest_framework import generics

from core.response import success_response, error_response

from ..models import School
from vs_rbac.permissions import IsVisionSuperAdmin
from ..serializers import (
    SchoolDetailSerializer,
    SchoolResetConfigSerializer,
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
    permission_classes = [IsVisionSuperAdmin]

    def post(self, request, *args, **kwargs):
        return self._run(request, SchoolResetConfigSerializer)
