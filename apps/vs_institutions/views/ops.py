from __future__ import annotations

from rest_framework import generics
from rest_framework.response import Response

from ..models import Institution
from ..permissions import IsVisionStaff, IsVisionSuperAdmin
from ..serializers import (
    InstitutionDetailSerializer,
    InstitutionResetConfigSerializer,
)


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = str(getattr(user, "id", "system"))
        return ctx


class _InstitutionOpBaseView(ActorContextMixin, generics.GenericAPIView):
    """Base for institution operation views."""
    queryset = Institution.objects.all()
    lookup_field = "slug"

    def _run(self, request, serializer_class):
        institution = self.get_object()
        serializer = serializer_class(
            data=request.data,
            context={**self.get_serializer_context(), "institution": institution},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        institution.refresh_from_db()
        return Response(InstitutionDetailSerializer(institution, context=self.get_serializer_context()).data)


class InstitutionResetConfigView(_InstitutionOpBaseView):
    permission_classes = [IsVisionSuperAdmin]

    def post(self, request, *args, **kwargs):
        return self._run(request, InstitutionResetConfigSerializer)
