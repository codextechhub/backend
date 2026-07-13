"""
Background-task monitoring for the admin console (engine-room view).

Reads core.BackgroundJob — the single source of truth for every Celery run
(written by core.tasks_base.TrackedTask). The owner-facing slice of the same
table lives at /v1/user/me/tasks/.

Endpoints (CX-staff only):
    GET /v1/admin/tasks/            all task runs (filters below)
    GET /v1/admin/tasks/stats/      counts by status + per-task breakdown
    GET /v1/admin/tasks/schedule/   the beat schedule + execution mode

List filters:
    ?status=QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED
    ?task=<substring of the task name>     e.g. ?task=import
    ?kind=import|export|email|system
    ?since=YYYY-MM-DD                      created on/after this date
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action

from core.models import BackgroundJob
from core.pagination import XVSPagination
from core.response import success_response

from .permissions import IsVisionStaff


class AdminJobSerializer(serializers.ModelSerializer):
    owner_name = serializers.SerializerMethodField()
    runtime_seconds = serializers.SerializerMethodField()

    class Meta:
        model = BackgroundJob
        fields = [
            "id", "celery_task_id", "task_name", "kind", "label",
            "owner", "owner_name", "tenant", "status", "progress", "worker",
            "created_at", "started_at", "finished_at", "runtime_seconds",
            "result", "error", "traceback",
        ]

    def get_owner_name(self, obj):
        return obj.owner.full_name if obj.owner_id and obj.owner else None

    def get_runtime_seconds(self, obj):
        if obj.started_at and obj.finished_at:
            return round((obj.finished_at - obj.started_at).total_seconds(), 3)
        return None


class TaskMonitorViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read-only window onto the full task history.

    docstring-name: Task monitor
    """

    permission_classes = [IsVisionStaff]
    serializer_class = AdminJobSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = BackgroundJob.objects.select_related("owner").order_by("-created_at")
        params = self.request.query_params

        status_param = (params.get("status") or "").strip().upper()
        if status_param:
            qs = qs.filter(status=status_param)

        task = (params.get("task") or "").strip()
        if task:
            qs = qs.filter(task_name__icontains=task)

        kind = (params.get("kind") or "").strip().lower()
        if kind:
            qs = qs.filter(kind=kind)

        since = (params.get("since") or "").strip()
        if since:
            qs = qs.filter(created_at__date__gte=since)

        return qs

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Status counts (all-time and last 24h) plus a per-task breakdown."""
        day_ago = timezone.now() - timedelta(hours=24)

        by_status = dict(
            BackgroundJob.objects.values_list("status").annotate(n=Count("id"))
        )
        last_24h = dict(
            BackgroundJob.objects.filter(created_at__gte=day_ago)
            .values_list("status").annotate(n=Count("id"))
        )
        by_task = list(
            BackgroundJob.objects.values("task_name")
            .annotate(runs=Count("id"))
            .order_by("-runs")[:20]
        )
        failures = list(
            BackgroundJob.objects.filter(status=BackgroundJob.Status.FAILED)
            .order_by("-finished_at")
            .values("task_name", "label", "finished_at", "celery_task_id")[:5]
        )
        return success_response(
            message="Task statistics retrieved.",
            data={
                "by_status": by_status,
                "last_24h": last_24h,
                "by_task": by_task,
                "recent_failures": failures,
                "total": BackgroundJob.objects.count(),
            },
        )

    @action(detail=False, methods=["get"])
    def schedule(self, request):
        """The beat schedule as configured in code, plus the execution mode."""
        from apps.celery import app as celery_app

        entries = [
            {
                "name": name,
                "task": entry["task"],
                "schedule": str(entry["schedule"]),
            }
            for name, entry in (celery_app.conf.beat_schedule or {}).items()
        ]
        return success_response(
            message="Beat schedule retrieved.",
            data={
                "eager_mode": bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)),
                "broker_configured": bool(getattr(settings, "CELERY_BROKER_URL", "")),
                "entries": entries,
            },
        )
