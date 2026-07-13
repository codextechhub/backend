"""
"My queues" — the owner-facing window onto background tasks.

Backs the frontend's Export → View Queues page:

    GET /v1/user/me/tasks/             my jobs (any authenticated user)
    GET /v1/user/me/tasks/?scope=all   every job incl. system/scheduled runs
                                       (requires the admin queue permission)
    GET /v1/user/me/tasks/summary/     counts for the page header + the
                                       can_view_all flag that controls the
                                       Mine/Admin toggle in the UI

List filters: ?status=QUEUED|RUNNING|SUCCEEDED|FAILED  ?kind=import|email|system
              ?since=YYYY-MM-DD
"""
from __future__ import annotations

from django.db.models import Count
from rest_framework import generics, serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView

from core.models import BackgroundJob
from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import IsAuthenticatedAndActive


def can_view_all_jobs(user) -> bool:
    """Admin-queue visibility: CX staff holding a platform admin role."""
    if getattr(user, "user_type", None) != "CX_STAFF":
        return False
    from vs_rbac.models import TenantUserRoleAssignment

    return TenantUserRoleAssignment.objects.filter(
        user=user,
        role__key__in=("xvs_super_admin", "xvs_platform_admin"),
        role__tenant__kind="PLATFORM",
        assignment_status="ACTIVE",
    ).exists()


class BackgroundJobSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    runtime_seconds = serializers.SerializerMethodField()

    class Meta:
        model = BackgroundJob
        fields = [
            "id", "kind", "label", "task_name", "status", "progress",
            "owner", "owner_name", "tenant",
            "created_at", "started_at", "finished_at", "runtime_seconds",
            "result", "error",
        ]

    def get_owner_name(self, obj):
        return obj.owner.full_name if obj.owner_id and obj.owner else None

    def get_runtime_seconds(self, obj):
        if obj.started_at and obj.finished_at:
            return round((obj.finished_at - obj.started_at).total_seconds(), 3)
        return None


def _scoped_queryset(request):
    qs = BackgroundJob.objects.select_related("owner").order_by("-created_at")

    scope = (request.query_params.get("scope") or "mine").strip().lower()
    if scope == "all":
        if not can_view_all_jobs(request.user):
            raise PermissionDenied("You do not have permission to view all queues.")
    else:
        qs = qs.filter(owner=request.user)

    status_param = (request.query_params.get("status") or "").strip().upper()
    if status_param:
        qs = qs.filter(status=status_param)

    kind = (request.query_params.get("kind") or "").strip().lower()
    if kind:
        qs = qs.filter(kind=kind)

    since = (request.query_params.get("since") or "").strip()
    if since:
        qs = qs.filter(created_at__date__gte=since)

    return qs


class MyTasksView(generics.ListAPIView):
    """docstring-name: My background tasks"""
    permission_classes = [IsAuthenticatedAndActive]
    serializer_class = BackgroundJobSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        return _scoped_queryset(self.request)


class MyTasksSummaryView(APIView):
    """docstring-name: My queue summary"""
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        qs = _scoped_queryset(request)
        by_status = dict(qs.values_list("status").annotate(n=Count("id")))
        return success_response(
            message="Queue summary retrieved.",
            data={
                "by_status": by_status,
                "total": sum(by_status.values()),
                "can_view_all": can_view_all_jobs(request.user),
            },
        )
