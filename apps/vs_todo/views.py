"""REST views for vs_todo. See urls.py for the full routing table.

The ToDo tool is gated to CX staff; visibility and assignment are then bounded
by the organogram — a person sees their own area and can only assign downward.
Those structural rules live in services/ (hierarchy, tasks); the views stay thin.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from core.mixins import XVSModelViewSetMixin
from core.response import success_response
from vs_rbac.permissions import IsAuthenticatedAndActive, IsVisionStaff
from vs_user.models import User

from .constants import TaskStatus
from .models import Task
from .serializers import (
    NodeDashboardSerializer, OrgRollupNodeSerializer, PersonSerializer,
    TaskSerializer, TaskWriteSerializer, ToggleSerializer,
)
from .services import dashboards as dashboards_svc
from .services import tasks as tasks_svc
from .services.hierarchy import TodoHierarchy
from .services.stats import own_tasks_qs, stats_for


# CX-staff intranet tool: authenticated, active, and a platform staff member.
TODO_PERMISSIONS = [IsAuthenticatedAndActive & IsVisionStaff]


def _resolve_focus(viewer: User, focus_id) -> User:
    """Return the person a manager wants to look at, enforcing area bounds.

    Defaults to the viewer. A different person is only allowed if they sit
    within the viewer's area (themselves or a report, at any depth).
    """
    if focus_id in (None, "", str(viewer.pk), viewer.pk):
        return viewer
    if int(focus_id) not in TodoHierarchy.area_user_ids(viewer):
        raise PermissionDenied("That person is not in your team.")
    return get_object_or_404(User, pk=focus_id)


# ── Tasks ─────────────────────────────────────────────────────────────────────

class TaskViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
    """CRUD for tasks plus the done/undone toggle.

    list      → the viewer's own tasks (the "My Tasks" screen), or a report's
                tasks via ?assignee=<id> (must be within the viewer's area).
                Filter by ?status=COMPLETED|IN_PROGRESS|OVERDUE.
    create    → self-set, or an assignment when assignee_id targets a report.

    docstring-name: ToDo tasks
    """
    serializer_class = TaskSerializer
    permission_classes = TODO_PERMISSIONS

    def get_queryset(self):
        viewer = self.request.user
        assignee_id = self.request.query_params.get("assignee")
        if assignee_id:
            if int(assignee_id) not in TodoHierarchy.area_user_ids(viewer):
                raise PermissionDenied("That person is not in your team.")
            qs = Task.objects.filter(assignee_id=assignee_id)
        else:
            qs = own_tasks_qs(viewer)
        qs = qs.select_related("assignee", "assigned_by")

        status_filter = self.request.query_params.get("status")
        if status_filter:
            wanted = status_filter.upper()
            if wanted in TaskStatus.values:
                # status is derived, so filter in Python on the (small) page set.
                qs = [t for t in qs if t.status == wanted]
        return qs

    def get_object(self):
        task = get_object_or_404(
            Task.objects.select_related("assignee", "assigned_by"),
            pk=self.kwargs["pk"],
        )
        if not tasks_svc.can_view_task(self.request.user, task):
            raise NotFound("No such task.")  # don't reveal existence outside area
        return task

    def create(self, request, *args, **kwargs):
        write = TaskWriteSerializer(data=request.data)
        write.is_valid(raise_exception=True)
        data = write.validated_data

        assignee = None
        if data.get("assignee_id"):
            assignee = get_object_or_404(User, pk=data["assignee_id"])

        task = tasks_svc.create_task(
            actor=request.user,
            title=data["title"],
            deadline=data["deadline"],
            assignee=assignee,
            description=data.get("description", ""),
            metric=data.get("metric", ""),
            target=data.get("target", ""),
            priority=data["priority"],
        )
        return success_response(
            message="Task created successfully.",
            data=TaskSerializer(task).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        task = self.get_object()
        if not tasks_svc.can_modify_task(request.user, task):
            raise PermissionDenied("You cannot edit this task.")
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        # Only the descriptive fields are editable here; ownership/assignment is
        # set at creation and not reshuffled through a plain PATCH.
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        task = self.get_object()
        if not tasks_svc.can_modify_task(request.user, task):
            raise PermissionDenied("You cannot delete this task.")
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        task = self.get_object()
        if not tasks_svc.can_modify_task(request.user, task):
            raise PermissionDenied("You cannot update this task.")
        ser = ToggleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tasks_svc.set_done(task, done=ser.validated_data["done"], actor=request.user)
        return success_response(
            message="Task updated successfully.",
            data=TaskSerializer(task).data,
        )


# ── Dashboards ────────────────────────────────────────────────────────────────

class MineView(APIView):
    """The "My Tasks" screen: the viewer's own tasks and their headline.

    docstring-name: My dashboard
    """
    permission_classes = TODO_PERMISSIONS

    def get(self, request):
        viewer = request.user
        tasks = list(own_tasks_qs(viewer).select_related("assignee", "assigned_by"))
        return success_response(
            message="Data retrieved successfully.",
            data={
                "person": PersonSerializer(viewer).data,
                "tasks": TaskSerializer(tasks, many=True).data,
                "stats": stats_for(tasks),
            },
        )


class TeamView(APIView):
    """The "My Team" screen, with optional ?focus=<user_id> drill-down.

    docstring-name: Team dashboard
    """
    permission_classes = TODO_PERMISSIONS

    def get(self, request):
        focus = _resolve_focus(request.user, request.query_params.get("focus"))
        payload = dashboards_svc.node_dashboard(focus)
        return success_response(
            message="Data retrieved successfully.",
            data=NodeDashboardSerializer(payload).data,
        )


class OrgView(APIView):
    """The "Organogram" screen: the viewer's tree with per-node roll-up stats.

    docstring-name: Organisation rollup
    """
    permission_classes = TODO_PERMISSIONS

    def get(self, request):
        tree = dashboards_svc.org_rollup(request.user)
        return success_response(
            message="Data retrieved successfully.",
            data=OrgRollupNodeSerializer(tree).data if tree else None,
        )


class AssignableView(APIView):
    """Who the viewer may assign a task to — everyone in their area below them.

    Powers the assignee picker in the assign modal (design: descendantsOf).

    docstring-name: Assignable staff
    """
    permission_classes = TODO_PERMISSIONS

    def get(self, request):
        people = TodoHierarchy.descendant_users(request.user)
        return success_response(
            message="Data retrieved successfully.",
            data=PersonSerializer(people, many=True).data,
        )
