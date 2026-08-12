from __future__ import annotations

from rest_framework import generics

from core.response import success_response, error_response

from ..models import Branch
from vs_rbac.permissions import (
    HasRBACPermission,
    IsAuthenticatedAndActive,
    IsVisionStaff,
)
from ..serializers import BranchDetailSerializer, BranchStateTransitionSerializer


class ActorContextMixin:
    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class BranchTransitionView(ActorContextMixin, generics.GenericAPIView):
    """docstring-name: Transition branch lifecycle"""
    # The granular key matches the sibling branch views (platform.branches.*);
    # `platform.branches.manage` is seeded as restricted/SENSITIVE and is
    # described in seed_platform_permissions as exactly this operation.
    # IsVisionStaff stays alongside it: suspending or closing a branch is a
    # platform commercial action, so a school-tenant role holding the key by
    # misconfiguration still must not reach it.
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "platform.branches.manage"
    serializer_class = BranchStateTransitionSerializer
    queryset = Branch.objects.all().select_related("school")
    lookup_field = "code"

    def get_queryset(self):
        # Branch codes are unique per school, not globally: without the slug
        # filter get_object() matches every school's branch N.
        qs = super().get_queryset()
        slug = self.kwargs.get("slug")
        if slug:
            qs = qs.filter(school__slug=slug)
        return qs

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
