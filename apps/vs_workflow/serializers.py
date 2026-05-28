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
    stages = WorkflowStageReadSerializer(many=True, read_only=True)
    routes = WorkflowRoutePathReadSerializer(many=True, read_only=True)

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
    eligible_approvers = WorkflowStageApproverReadSerializer(many=True, read_only=True)
    actions = WorkflowStageActionReadSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowStageInstance
        fields = [
            "id", "stage_code", "stage_label", "stage_kind", "status",
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
            "requested_by", "submitted_at", "completed_at",
        ]


class WorkflowInstanceDetailSerializer(WorkflowInstanceListSerializer):
    stage_instances = WorkflowStageInstanceReadSerializer(many=True, read_only=True)
    audit_logs      = WorkflowAuditLogReadSerializer(many=True, read_only=True)

    class Meta(WorkflowInstanceListSerializer.Meta):
        fields = WorkflowInstanceListSerializer.Meta.fields + ["stage_instances", "audit_logs"]


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
