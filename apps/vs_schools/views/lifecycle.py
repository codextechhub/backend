from __future__ import annotations

from rest_framework import generics

from core.response import success_response, error_response

from ..models import Branch
from vs_rbac.permissions import IsVisionStaff, IsAuthenticatedAndActive
from ..serializers import BranchDetailSerializer, BranchStateTransitionSerializer


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class BranchTransitionView(ActorContextMixin, generics.GenericAPIView):
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = BranchStateTransitionSerializer
    queryset = Branch.objects.all()
    lookup_field = "code"

    def post(self, request, *args, **kwargs):
        branch = self.get_object()
        serializer = self.get_serializer(data=request.data, context={**self.get_serializer_context(), "branch": branch, "school": branch.school})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        branch.refresh_from_db()
        return success_response(
            message="Branch state updated successfully.",
            data=BranchDetailSerializer(branch, context=self.get_serializer_context()).data,
        )
