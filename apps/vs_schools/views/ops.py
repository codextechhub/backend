from __future__ import annotations

from rest_framework import generics
from rest_framework.response import Response

from ..models import School
from ..permissions import IsVisionStaff, IsVisionSuperAdmin
from ..serializers import (
    SchoolDetailSerializer,
    SchoolResetConfigSerializer,
)


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = getattr(user, "id", "system")
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
        return Response(SchoolDetailSerializer(school, context=self.get_serializer_context()).data)


class SchoolResetConfigView(_SchoolOpBaseView):
    permission_classes = [IsVisionSuperAdmin]

    def post(self, request, *args, **kwargs):
        return self._run(request, SchoolResetConfigSerializer)
