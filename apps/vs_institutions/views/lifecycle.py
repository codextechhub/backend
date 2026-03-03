from __future__ import annotations

from rest_framework import generics
from rest_framework.response import Response

from ..models import Institution
from ..permissions import IsVisionStaff
from ..serializers import InstitutionDetailSerializer, InstitutionStateTransitionSerializer


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = str(getattr(user, "id", "system"))
        return ctx


class InstitutionTransitionView(ActorContextMixin, generics.GenericAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionStateTransitionSerializer
    queryset = Institution.objects.all()
    lookup_field = "slug"

    def post(self, request, *args, **kwargs):
        institution = self.get_object()
        serializer = self.get_serializer(data=request.data, context={**self.get_serializer_context(), "institution": institution})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        institution.refresh_from_db()
        return Response(InstitutionDetailSerializer(institution, context=self.get_serializer_context()).data)
