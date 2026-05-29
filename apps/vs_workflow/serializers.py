"""DRF serializers for vs_workflow REST surface."""

from rest_framework import serializers

from vs_workflow.models import (
    ApprovalDelegation, WorkflowAuditLog, WorkflowInstance,
    WorkflowRoutePath, WorkflowStage, WorkflowStageAction,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)


class WorkflowStageReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStage
        fields = [
            "id", "code", "label", "kind", "order",
            "approver_permission_key", "approver_scope",
            "advance_rule", "quorum_count", "on_rejection",
            "skip_if_no_approvers", "inclusion_condition",
        ]


class WorkflowRoutePathReadSerializer(serializers.ModelSerializer):
    from_stage_code = serializers.CharField(source="from_stage.code", read_only=True, default=None)
    to_stage_code   = serializers.CharField(source="to_stage.code",   read_only=True, default=None)

    class Meta:
        model = WorkflowRoutePath
        fields = ["id", "from_stage_code", "to_stage_code", "order", "condition"]


class WorkflowTemplateReadSerializer(serializers.ModelSerializer):
    stages = serializers.SerializerMethodField()
    routes = WorkflowRoutePathReadSerializer(many=True, read_only=True)

    def get_stages(self, obj):
        active = obj.stages.filter(retired_at__isnull=True).order_by("order")
        return WorkflowStageReadSerializer(active, many=True).data

    class Meta:
        model = WorkflowTemplate
        fields = [
            "id", "school", "branch", "document_type", "code",
            "name", "description", "notification_events",
            "created_at", "updated_at", "stages", "routes",
        ]


class WorkflowTemplatePublishSerializer(serializers.Serializer):
    document_type       = serializers.CharField(max_length=100)
    code                = serializers.SlugField(max_length=100)
    name                = serializers.CharField(max_length=200)
    description         = serializers.CharField(required=False, allow_blank=True, default="")
    notification_events = serializers.DictField(child=serializers.BooleanField(),
                                                required=False, default=dict)
    stages  = serializers.ListField(child=serializers.DictField())
    routes  = serializers.ListField(child=serializers.DictField(), required=False, default=list)

    def validate_stages(self, value):
        """Reject unknown enum values (e.g. on_rejection='STOP') up front, rather
        than silently mis-routing at vote time."""
        from vs_workflow.constants import (
            ApproverScope, StageAdvanceRule, StageKind, StageOnRejection,
        )
        allowed = {
            "kind": {c.value for c in StageKind},
            "approver_scope": {c.value for c in ApproverScope},
            "advance_rule": {c.value for c in StageAdvanceRule},
            "on_rejection": {c.value for c in StageOnRejection},
        }
        if not value:
            raise serializers.ValidationError("At least one stage is required.")
        for i, s in enumerate(value):
            label = s.get("code") or f"#{i + 1}"
            if not s.get("code") or not s.get("label"):
                raise serializers.ValidationError(f"Stage {label}: 'code' and 'label' are required.")
            for field, choices in allowed.items():
                if field in s and s[field] not in choices:
                    raise serializers.ValidationError(
                        f"Stage '{label}': invalid {field} '{s[field]}'. "
                        f"Allowed: {', '.join(sorted(choices))}."
                    )
        return value


class WorkflowStageActionReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStageAction
        fields = [
            "id", "action", "actor", "on_behalf_of", "comment", "attempt",
            "acted_at", "reversed_at", "reversed_by", "reversal_reason", "is_reversal_of",
        ]


class WorkflowStageApproverReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStageApprover
        fields = ["id", "user", "on_behalf_of", "attempt", "recorded_at"]


class WorkflowStageInstanceReadSerializer(serializers.ModelSerializer):
    stage_code  = serializers.CharField(source="stage.code",  read_only=True)
    stage_label = serializers.CharField(source="stage.label", read_only=True)
    stage_kind  = serializers.CharField(source="stage.kind",  read_only=True)
    on_rejection = serializers.CharField(source="stage.on_rejection", read_only=True)
    advance_rule = serializers.CharField(source="stage.advance_rule", read_only=True)
    quorum_count = serializers.IntegerField(source="stage.quorum_count", read_only=True)
    eligible_approvers = WorkflowStageApproverReadSerializer(many=True, read_only=True)
    actions = WorkflowStageActionReadSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowStageInstance
        fields = [
            "id", "stage_code", "stage_label", "stage_kind", "status",
            "on_rejection", "advance_rule", "quorum_count",
            "activated_at", "resolved_at", "skip_reason", "attempt",
            "eligible_approvers", "actions",
        ]


class WorkflowAuditLogReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowAuditLog
        fields = ["id", "event_type", "actor", "stage_instance",
                  "context", "message", "occurred_at"]


class WorkflowInstanceListSerializer(serializers.ModelSerializer):
    template_code       = serializers.CharField(source="template.code",  read_only=True)
    current_stage_code  = serializers.CharField(source="current_stage.code",  read_only=True, default=None)
    current_stage_label = serializers.CharField(source="current_stage.label", read_only=True, default=None)

    class Meta:
        model = WorkflowInstance
        fields = [
            "id", "document_type", "document_object_id",
            "template_code",
            "status", "current_stage_code", "current_stage_label",
            "requested_by", "submitted_at", "completed_at", "updated_at",
        ]


class WorkflowInstanceDetailSerializer(WorkflowInstanceListSerializer):
    stage_instances = WorkflowStageInstanceReadSerializer(many=True, read_only=True)
    audit_logs      = WorkflowAuditLogReadSerializer(many=True, read_only=True)
    next_stage      = serializers.SerializerMethodField()

    class Meta(WorkflowInstanceListSerializer.Meta):
        fields = WorkflowInstanceListSerializer.Meta.fields + [
            "document_summary", "next_stage", "stage_instances", "audit_logs",
        ]

    def get_next_stage(self, obj):
        from vs_workflow.services.routing import preview_next_approval_stage
        return preview_next_approval_stage(obj)


class SubmitForApprovalSerializer(serializers.Serializer):
    content_type_id = serializers.IntegerField()
    object_id       = serializers.CharField(max_length=64)
    template_code   = serializers.CharField(required=False, allow_blank=True, default="")


class StageActionWriteSerializer(serializers.Serializer):
    action  = serializers.ChoiceField(choices=["APPROVED", "REJECTED", "RETURNED"])
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class CancelInstanceSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ReverseActionSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ApprovalDelegationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovalDelegation
        fields = [
            "id", "delegator", "delegate", "starts_at", "ends_at",
            "document_type", "exclusive", "reason", "created_at", "revoked_at",
        ]
        read_only_fields = ["id", "created_at", "revoked_at"]
