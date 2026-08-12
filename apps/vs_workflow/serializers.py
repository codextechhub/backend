"""DRF serializers for vs_workflow REST surface."""

from rest_framework import serializers

from vs_workflow.constants import (
    ApproverScope, ApproverSource, GroupMemberKind, OrganogramTarget,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverGroup, WorkflowApproverGroupMember,
    WorkflowStageDynamicRule,
    WorkflowAuditLog, WorkflowInstance,
    WorkflowRoutePath, WorkflowStage, WorkflowStageAction,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)


class WorkflowStageDynamicRuleReadSerializer(serializers.ModelSerializer):
    role_key  = serializers.CharField(source="role.key",  read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    is_fallback = serializers.BooleanField(read_only=True)

    class Meta:
        model = WorkflowStageDynamicRule
        fields = ["id", "order", "condition", "role_key", "role_name",
                  "label", "is_fallback"]


class WorkflowStageReadSerializer(serializers.ModelSerializer):
    organogram_position_code = serializers.CharField(
        source="organogram_position.code", read_only=True, default=None,
    )
    approver_role_key = serializers.CharField(
        source="approver_role.key", read_only=True, default=None,
    )
    approver_role_name = serializers.CharField(
        source="approver_role.name", read_only=True, default=None,
    )
    approver_group_code = serializers.CharField(
        source="approver_group.code", read_only=True, default=None,
    )
    approver_group_name = serializers.CharField(
        source="approver_group.name", read_only=True, default=None,
    )
    dynamic_role_rules = WorkflowStageDynamicRuleReadSerializer(
        source="dynamic_rules", many=True, read_only=True,
    )

    class Meta:
        model = WorkflowStage
        fields = [
            "id", "code", "label", "kind", "order",
            "approver_source",
            "approver_permission_key", "approver_scope",
            "approver_role_key", "approver_role_name",
            "approver_group_code", "approver_group_name",
            "dynamic_role_rules",
            "organogram_target", "organogram_levels", "organogram_position_code",
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
            "id", "tenant", "branch", "document_type", "code",
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
            ApproverScope, ApproverSource, OrganogramTarget,
            StageAdvanceRule, StageKind, StageOnRejection,
        )
        allowed = {
            "kind": {c.value for c in StageKind},
            "approver_source": {c.value for c in ApproverSource},
            "approver_scope": {c.value for c in ApproverScope},
            "organogram_target": {c.value for c in OrganogramTarget},
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
            # When using the organogram strategy, a climb mode is mandatory and
            # SPECIFIC_POSITION additionally needs a position code.
            if s.get("approver_source") == ApproverSource.ORGANOGRAM.value:
                target = s.get("organogram_target")
                if not target:
                    raise serializers.ValidationError(
                        f"Stage '{label}': organogram_target is required when "
                        f"approver_source is ORGANOGRAM."
                    )
                if target == OrganogramTarget.SPECIFIC_POSITION.value and not s.get("organogram_position_code"):
                    raise serializers.ValidationError(
                        f"Stage '{label}': organogram_position_code is required "
                        f"when organogram_target is SPECIFIC_POSITION."
                    )
            # A ROLE stage must name the tenant role it resolves; existence of
            # the role is checked tenant-aware in the publish service.
            if s.get("approver_source") == ApproverSource.ROLE.value and not s.get("approver_role_key"):
                raise serializers.ValidationError(
                    f"Stage '{label}': approver_role_key is required when "
                    f"approver_source is ROLE."
                )
            # Likewise a group stage must name its approver group.
            if s.get("approver_source") == ApproverSource.WORKFLOW_GROUP.value and \
                    not s.get("approver_group_code"):
                raise serializers.ValidationError(
                    f"Stage '{label}': approver_group_code is required when "
                    f"approver_source is WORKFLOW_GROUP."
                )
            # A dynamic stage needs rules. Their roles and conditions are
            # validated tenant-aware in the publish service.
            if s.get("approver_source") == ApproverSource.DYNAMIC_ROLE.value:
                rules = s.get("dynamic_role_rules")
                if not rules or not isinstance(rules, list):
                    raise serializers.ValidationError(
                        f"Stage '{label}': dynamic_role_rules must be a non-empty "
                        f"list when approver_source is DYNAMIC_ROLE."
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
        # delegator is set from request.user in the view's perform_create - it
        # must be read-only so DRF validation doesn't require the client to send it.
        read_only_fields = ["id", "delegator", "created_at", "revoked_at"]


class ApproverPreviewRequestSerializer(serializers.Serializer):
    """Validates an ad-hoc stage config + sample requester for the approver
    preview endpoint. Mirrors the WorkflowStage approver fields so a template
    builder can ask "who would approve?" without persisting anything."""

    requester = serializers.CharField(help_text="User id of the sample requester.")
    approver_source = serializers.ChoiceField(
        choices=ApproverSource.choices, default=ApproverSource.RBAC_PERMISSION,
    )
    # ORGANOGRAM config
    organogram_target = serializers.ChoiceField(
        choices=OrganogramTarget.choices, required=False, allow_blank=True, default="",
    )
    organogram_levels = serializers.IntegerField(required=False, min_value=1, default=1)
    # A Position *code* (matches the publish payload's organogram_position_code).
    organogram_position_code = serializers.CharField(required=False, allow_blank=True, default="")
    # RBAC config
    approver_permission_key = serializers.CharField(required=False, allow_blank=True, default="")
    approver_scope = serializers.ChoiceField(
        choices=ApproverScope.choices, required=False, default=ApproverScope.PLATFORM,
    )
    # ROLE config - a TenantRoleTemplate *key* (matches the publish payload's
    # approver_role_key).
    approver_role_key = serializers.CharField(required=False, allow_blank=True, default="")
    # WORKFLOW_GROUP config - an approver group *code*.
    approver_group_code = serializers.CharField(required=False, allow_blank=True, default="")
    # DYNAMIC_ROLE config: the rules to try, plus the sample document to try
    # them against, so a builder can check "a 150,000 request goes to the
    # Bursar" before publishing anything.
    dynamic_role_rules = serializers.ListField(
        child=serializers.DictField(), required=False, default=list)
    sample_document = serializers.DictField(required=False, default=dict)
    # Optional context for delegation matching.
    document_type = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["approver_source"] == ApproverSource.ORGANOGRAM:
            if not attrs.get("organogram_target"):
                raise serializers.ValidationError(
                    {"organogram_target": "Required when approver_source is ORGANOGRAM."})
            if attrs["organogram_target"] == OrganogramTarget.SPECIFIC_POSITION and not attrs.get("organogram_position_code"):
                raise serializers.ValidationError(
                    {"organogram_position_code": "Required when target is SPECIFIC_POSITION."})
        elif attrs["approver_source"] == ApproverSource.ROLE:
            if not attrs.get("approver_role_key"):
                raise serializers.ValidationError(
                    {"approver_role_key": "Required when approver_source is ROLE."})
        elif attrs["approver_source"] == ApproverSource.WORKFLOW_GROUP:
            if not attrs.get("approver_group_code"):
                raise serializers.ValidationError(
                    {"approver_group_code": "Required when approver_source is WORKFLOW_GROUP."})
        elif attrs["approver_source"] == ApproverSource.DYNAMIC_ROLE:
            if not attrs.get("dynamic_role_rules"):
                raise serializers.ValidationError(
                    {"dynamic_role_rules": "Required when approver_source is DYNAMIC_ROLE."})
        elif not attrs.get("approver_permission_key"):
            raise serializers.ValidationError(
                {"approver_permission_key": "Required when approver_source is RBAC_PERMISSION."})
        return attrs


# ── Approver groups (the "Workflow Approver" screen) ─────────────────────────

class WorkflowApproverGroupMemberReadSerializer(serializers.ModelSerializer):
    """One membership row, with the display fields the screen needs.

    Live resolution ("resolves to N people") is not computed here - it is
    served per group by the group detail/resolve endpoints, so listing many
    groups does not run one resolution query per member row.
    """

    role_key      = serializers.CharField(source="role.key",       read_only=True, default=None)
    role_name     = serializers.CharField(source="role.name",      read_only=True, default=None)
    position_code = serializers.CharField(source="position.code",  read_only=True, default=None)
    position_title = serializers.CharField(source="position.title", read_only=True, default=None)
    user_name     = serializers.SerializerMethodField()
    user_email    = serializers.CharField(source="user.email",     read_only=True, default=None)

    class Meta:
        model = WorkflowApproverGroupMember
        fields = [
            "id", "kind", "user", "user_name", "user_email",
            "role", "role_key", "role_name",
            "position", "position_code", "position_title", "added_at",
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        if obj.user is None:
            return None
        return getattr(obj.user, "full_name", "") or obj.user.get_username()


class WorkflowApproverGroupSerializer(serializers.ModelSerializer):
    members = WorkflowApproverGroupMemberReadSerializer(many=True, read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowApproverGroup
        fields = [
            "id", "code", "name", "description", "branch", "is_active",
            "members", "member_count", "created_at", "updated_at",
        ]
        # tenant and created_by come from the request, never the payload.
        read_only_fields = ["id", "members", "created_at", "updated_at"]

    def get_member_count(self, obj):
        return obj.members.count()

    def validate_code(self, value):
        """Codes are the stable handle templates publish against, so they must
        stay unique per tenant and immutable once a group exists."""
        tenant = self.context.get("tenant")
        if self.instance is not None:
            if value != self.instance.code:
                raise serializers.ValidationError(
                    "A group's code cannot be changed - templates reference it. "
                    "Create a new group instead.")
            return value
        if tenant is not None and WorkflowApproverGroup.all_objects.filter(
                tenant=tenant, code=value).exists():
            raise serializers.ValidationError(
                f"An approver group with code '{value}' already exists in this tenant.")
        return value

    def validate_branch(self, value):
        tenant = self.context.get("tenant")
        if value is not None and tenant is not None and value.school.tenant_id != tenant.pk:
            raise serializers.ValidationError("Branch must belong to your tenant.")
        return value


class WorkflowApproverGroupMemberWriteSerializer(serializers.Serializer):
    """Adds one member to a group. Exactly one target must match `kind`.

    Targets are validated against the group's tenant here rather than trusted:
    the add-member combobox is a tenant-scoped search, but the API is the
    boundary that has to hold.
    """

    kind = serializers.ChoiceField(choices=GroupMemberKind.choices)
    user = serializers.CharField(required=False, allow_blank=True, default="")
    role_key = serializers.CharField(required=False, allow_blank=True, default="")
    position_code = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        kind = attrs["kind"]
        tenant = self.context["tenant"]
        required = {
            GroupMemberKind.USER: "user",
            GroupMemberKind.ROLE: "role_key",
            GroupMemberKind.POSITION: "position_code",
        }[kind]
        if not attrs.get(required):
            raise serializers.ValidationError({required: f"Required when kind is {kind}."})

        if kind == GroupMemberKind.USER:
            from django.contrib.auth import get_user_model
            user = get_user_model().objects.filter(
                pk=attrs["user"], tenant=tenant, is_active=True).first()
            if user is None:
                # Same message for "not found" and "other tenant" - the API must
                # not confirm that a user id exists elsewhere.
                raise serializers.ValidationError(
                    {"user": "No active user with that id exists in your tenant."})
            attrs["resolved_target"] = user
        elif kind == GroupMemberKind.ROLE:
            from vs_rbac.models import TenantRoleTemplate
            role = TenantRoleTemplate.objects.filter(
                tenant=tenant, key=attrs["role_key"],
                status=TenantRoleTemplate.Status.ACTIVE).first()
            if role is None:
                raise serializers.ValidationError(
                    {"role_key": "No active role with that key exists in your tenant."})
            attrs["resolved_target"] = role
        else:
            try:
                from vs_user.models import Position
            except ImportError:
                raise serializers.ValidationError(
                    {"position_code": "The organogram is not available in this install."})
            position = Position.objects.filter(
                code=attrs["position_code"], is_active=True).first()
            if position is None:
                raise serializers.ValidationError(
                    {"position_code": "No active position with that code exists."})
            attrs["resolved_target"] = position
        return attrs
