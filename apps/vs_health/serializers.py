"""Serializers for the manageable vs_health entities.

The dashboard/analytics endpoints return pre-computed dicts from
``vs_health.services`` and don't need serializers; these cover the CRUD
surfaces (incidents, alert rules, deployments) and a couple of read shapes.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    MonitoredService,
    Incident,
    IncidentEvent,
    AlertRule,
    Alert,
    Deployment,
)


class MonitoredServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = MonitoredService
        fields = ["id", "key", "name", "group", "tier", "kind",
                  "current_status", "status_changed_at", "is_active"]
        read_only_fields = fields


class IncidentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentEvent
        fields = ["id", "kind", "who", "text", "created_at"]
        read_only_fields = ["id", "created_at"]


class IncidentEventCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentEvent
        fields = ["kind", "who", "text"]


class IncidentListSerializer(serializers.ModelSerializer):
    severity_label = serializers.CharField(source="get_severity_display", read_only=True)
    service_keys = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = ["id", "code", "title", "severity", "severity_label", "status",
                  "source", "owner_label", "team", "service_keys",
                  "affected_tenant_count", "started_at", "resolved_at"]

    def get_service_keys(self, obj):
        return [s.key for s in obj.services.all()]


class IncidentDetailSerializer(IncidentListSerializer):
    timeline = IncidentEventSerializer(many=True, read_only=True)

    class Meta(IncidentListSerializer.Meta):
        fields = IncidentListSerializer.Meta.fields + [
            "summary", "postmortem", "acknowledged_at", "timeline", "created_at", "updated_at",
        ]


class IncidentCreateUpdateSerializer(serializers.ModelSerializer):
    """Manual incident authoring. ``code`` is auto-assigned when omitted."""
    services = serializers.SlugRelatedField(
        slug_field="key", queryset=MonitoredService.objects.all(),
        many=True, required=False,
    )

    class Meta:
        model = Incident
        fields = ["code", "title", "severity", "status", "owner_label", "team",
                  "services", "summary", "postmortem", "started_at",
                  "resolved_at", "acknowledged_at"]
        extra_kwargs = {"code": {"required": False}}

    def create(self, validated_data):
        from .tasks import _next_incident_code
        if not validated_data.get("code"):
            validated_data["code"] = _next_incident_code()
        services = validated_data.pop("services", [])
        incident = Incident.objects.create(source=Incident.Source.MANUAL, **validated_data)
        if services:
            incident.services.set(services)
        incident.add_event(kind="opened", who=incident.owner_label or "Operator",
                           text="Incident opened manually.")
        return incident

    def update(self, instance, validated_data):
        services = validated_data.pop("services", None)
        prev_status = instance.status
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if services is not None:
            instance.services.set(services)
        if "status" in validated_data and validated_data["status"] != prev_status:
            instance.add_event(kind="status", who=instance.owner_label or "Operator",
                               text=f"Status changed {prev_status} → {instance.status}.")
        return instance


class AlertRuleSerializer(serializers.ModelSerializer):
    target_service_key = serializers.SlugRelatedField(
        source="target_service", slug_field="key",
        queryset=MonitoredService.objects.all(), required=False, allow_null=True,
    )

    class Meta:
        model = AlertRule
        fields = ["id", "name", "metric", "comparator", "threshold", "duration_sec",
                  "severity", "target_service_key", "target_queue", "channel", "is_enabled"]


class AlertSerializer(serializers.ModelSerializer):
    rule_name = serializers.CharField(source="rule.name", read_only=True)
    service_key = serializers.CharField(source="service.key", read_only=True, default=None)

    class Meta:
        model = Alert
        fields = ["id", "rule_name", "severity", "title", "service_key",
                  "value", "threshold", "status", "fired_at", "resolved_at", "incident_id"]
        read_only_fields = fields


class DeploymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deployment
        fields = ["id", "version", "environment", "kind", "actor", "text", "deployed_at"]
        read_only_fields = ["id"]


class TaskRowSerializer(serializers.Serializer):
    """Read shape for the Background Jobs task table (reads core.BackgroundJob)."""
    id = serializers.UUIDField(read_only=True)
    task_name = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.CharField()
    queue = serializers.SerializerMethodField()
    status = serializers.CharField()
    tenant = serializers.SerializerMethodField()
    duration_sec = serializers.SerializerMethodField()
    worker = serializers.CharField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField()

    def get_queue(self, obj):
        from .tasks import KIND_TO_QUEUE
        return KIND_TO_QUEUE.get((obj.kind or "").lower(), "celery")

    def get_tenant(self, obj):
        return obj.school.name if obj.school_id else None

    def get_duration_sec(self, obj):
        if obj.started_at and obj.finished_at:
            return round((obj.finished_at - obj.started_at).total_seconds(), 1)
        return None
