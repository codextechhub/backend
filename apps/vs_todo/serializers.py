"""DRF serializers for the vs_todo REST surface.

Read serializers shape the task rows and the roll-up dashboards the design
renders (rings, pills, breadcrumbs). Write serializers validate task creation
and assignment; the actual hierarchy rules live in services/tasks.py.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_user.models import User

from .constants import Priority
from .models import Task


# ── People (compact, derived from the organogram) ─────────────────────────────

class PersonSerializer(serializers.ModelSerializer):
    """The minimal person card the design draws: name, role, initials, dept."""
    name     = serializers.CharField(source="full_name", read_only=True)
    role     = serializers.CharField(read_only=True)
    initials = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name", "role", "initials"]

    def get_initials(self, obj) -> str:
        parts = (obj.full_name or obj.email).split()
        return "".join(p[0] for p in parts[:2]).upper()


# ── Tasks ─────────────────────────────────────────────────────────────────────

class TaskSerializer(serializers.ModelSerializer):
    """Full read view of a task, with derived status and people expanded."""
    assignee    = PersonSerializer(read_only=True)
    assigned_by = PersonSerializer(read_only=True)
    status      = serializers.CharField(read_only=True)
    is_self_set = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = [
            "id", "title", "description", "metric", "target",
            "deadline", "priority", "department",
            "assignee", "assigned_by", "assigned_by_name",
            "is_done", "completed_at", "status", "is_self_set",
            "created_at", "updated_at",
        ]


class TaskWriteSerializer(serializers.Serializer):
    """Create / assign a task.

    ``assignee_id`` omitted (or equal to the actor) → a self-set task. Otherwise
    it is an assignment, and the service enforces that the target is within the
    actor's area. The view passes the authenticated user in as ``actor``.
    """
    title       = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    metric      = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    target      = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    deadline    = serializers.DateField()
    priority    = serializers.ChoiceField(choices=Priority.choices, default=Priority.MEDIUM)
    assignee_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_assignee_id(self, value):
        if value is None:
            return value
        if not User.objects.filter(pk=value).exists():
            raise serializers.ValidationError("No such user.")
        return value


class ToggleSerializer(serializers.Serializer):
    """Mark a task done / not-done."""
    done = serializers.BooleanField()


# ── Dashboards (roll-up) ──────────────────────────────────────────────────────

class StatsSerializer(serializers.Serializer):
    total       = serializers.IntegerField()
    done        = serializers.IntegerField()
    in_progress = serializers.IntegerField()
    overdue     = serializers.IntegerField()
    pct         = serializers.IntegerField()


class ReportCardSerializer(serializers.Serializer):
    """One direct report's headline on a manager's team dashboard — the person
    plus their *area* roll-up (themselves + everyone beneath them)."""
    person      = PersonSerializer()
    is_manager  = serializers.BooleanField()
    area_stats  = StatsSerializer()


class NodeDashboardSerializer(serializers.Serializer):
    """The full dashboard for one person (own tasks + area roll-up + reports)."""
    person     = PersonSerializer()
    is_manager = serializers.BooleanField()
    own_tasks  = TaskSerializer(many=True)
    own_stats  = StatsSerializer()
    area_stats = StatsSerializer()
    reports    = ReportCardSerializer(many=True)
    breadcrumb = PersonSerializer(many=True)


class OrgRollupNodeSerializer(serializers.Serializer):
    """A node in the organogram roll-up tree — recurses into direct_reports."""
    person         = PersonSerializer()
    is_manager     = serializers.BooleanField()
    own_stats      = StatsSerializer()
    area_stats     = StatsSerializer()
    direct_reports = serializers.SerializerMethodField()

    def get_direct_reports(self, obj):
        return OrgRollupNodeSerializer(obj.get("direct_reports", []), many=True).data
