"""DRF serializers for vs_workflow REST surface."""

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers
from core.person_exit import person_is_exited, prime_exit_states

from vs_workflow.conditions.fields import document_type_label

from vs_rbac.serializers.tenant import (
    USER_NOT_FOUND, TenantScopedRelatedField, TenantScopedSerializerMixin,
)
from vs_workflow.constants import (
    ApproverScope, ApproverSource, GroupMemberKind, OrganogramTarget,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverGroup, WorkflowApproverGroupMember,
    WorkflowDynamicRole, WorkflowDynamicRoleRule,
    WorkflowStageApproverOverride, WorkflowStageDynamicRule,
    WorkflowAuditLog, WorkflowInstance,
    WorkflowRoutePath, WorkflowStage, WorkflowStageAction,
    WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)


class WorkflowNamedUserListSerializer(serializers.ListSerializer):
    """Prime linked users once for a rule or group-member list."""

    def to_representation(self, data):
        rows = list(data.all()) if hasattr(data, "all") else list(data)
        prime_exit_states(self.context, (getattr(row, "user_id", None) for row in rows))
        return super().to_representation(rows)


class WorkflowContainerPeopleListSerializer(serializers.ListSerializer):
    """Prime users nested under a page of approver groups or Dynamic Roles."""

    def to_representation(self, data):
        rows = list(data.all()) if hasattr(data, "all") else list(data)
        ids = set()
        for row in rows:
            related = getattr(row, "members", None) or getattr(row, "rules", None)
            if related is not None:
                ids.update(item.user_id for item in related.all() if item.user_id)
        prime_exit_states(self.context, ids)
        return super().to_representation(rows)


class WorkflowDelegationPeopleListSerializer(serializers.ListSerializer):
    """Prime every person named by a delegation page in one lookup."""

    def to_representation(self, data):
        rows = list(data.all()) if hasattr(data, "all") else list(data)
        prime_exit_states(self.context, (
            user_id for row in rows
            for user_id in (row.delegator_id, row.delegate_id, row.created_by_id)
        ))
        return super().to_representation(rows)


class WorkflowStageDynamicRuleReadSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source="role.name", read_only=True, default=None)
    is_fallback = serializers.BooleanField(read_only=True)

    class Meta:
        model = WorkflowStageDynamicRule
        fields = ["id", "order", "condition", "role_key", "role_name",
                  "label", "is_fallback"]


class WorkflowDynamicRoleRuleReadSerializer(serializers.ModelSerializer):
    """One Dynamic Role rule, with the names the screen shows for its target."""

    role_name = serializers.CharField(source="role.name", read_only=True, default=None)
    user_name = serializers.SerializerMethodField()
    user_is_exited = serializers.SerializerMethodField()
    group_code = serializers.CharField(source="group.code", read_only=True, default=None)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    is_fallback = serializers.BooleanField(read_only=True)

    class Meta:
        model = WorkflowDynamicRoleRule
        list_serializer_class = WorkflowNamedUserListSerializer
        fields = ["id", "order", "condition", "target_kind", "role_key", "role_name",
                  "user", "user_name", "user_is_exited", "group", "group_code", "group_name",
                  "label", "is_fallback"]
        read_only_fields = fields

    def get_user_name(self, obj):
        if obj.user is None:
            return None
        return getattr(obj.user, "full_name", "") or obj.user.get_username()

    def get_user_is_exited(self, obj):
        return person_is_exited(self.context, obj.user_id)


class WorkflowDynamicRoleSummarySerializer(serializers.ModelSerializer):
    """A Dynamic Role as a stage shows it: its name, and the rules it runs."""

    rules = WorkflowDynamicRoleRuleReadSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowDynamicRole
        fields = ["id", "code", "name", "is_active", "document_types", "rules"]
        read_only_fields = fields


class WorkflowStageReadSerializer(serializers.ModelSerializer):
    """One stage as the template builder reads it.

    ``organogram_position_code`` is the current code of whichever post the stage
    names, on the CX chart or on its tenant's own. Posts on a tenant's chart are
    described in one lookup for the whole template: the caller passes that map
    as ``position_labels`` (see ``WorkflowTemplateReadSerializer.get_stages``),
    and a stage serialized without it looks its own post up.
    """

    organogram_position_code = serializers.SerializerMethodField()
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
    dynamic_role = WorkflowDynamicRoleSummarySerializer(read_only=True)

    class Meta:
        model = WorkflowStage
        fields = [
            "id", "code", "label", "kind", "order",
            "approver_source",
            "approver_scope",
            "approver_role_key", "approver_role_name",
            "approver_group_code", "approver_group_name",
            "dynamic_role_rules", "dynamic_role",
            "organogram_target", "organogram_levels", "organogram_position_code",
            "advance_rule", "quorum_count", "on_rejection",
            "skip_if_no_approvers", "inclusion_condition",
        ]

    def get_organogram_position_code(self, obj):
        if obj.organogram_position_id:
            return obj.organogram_position.code
        if obj.organogram_tenant_position_id is None:
            return None
        labels = self.context.get("position_labels")
        if labels is None:
            from vs_workflow.services.positions import describe_position

            label = describe_position(None, obj.organogram_tenant_position_id,
                                      obj.template.tenant)
        else:
            label = labels.get(obj.organogram_tenant_position_id)
        return label[0] if label else None


class WorkflowRoutePathReadSerializer(serializers.ModelSerializer):
    from_stage_code = serializers.CharField(source="from_stage.code", read_only=True, default=None)
    to_stage_code   = serializers.CharField(source="to_stage.code",   read_only=True, default=None)

    class Meta:
        model = WorkflowRoutePath
        fields = ["id", "from_stage_code", "to_stage_code", "order", "condition"]


class WorkflowTemplateReadSerializer(serializers.ModelSerializer):
    """One template, plus where it stands between the platform and this tenant.

    A screen has to be able to say "you are running the platform's version" or
    "you are running your own, and the platform's has moved on since" without
    guessing. Those answers need the *other* template of the same
    (document_type, code), which the view resolves once for the whole page and
    passes in as ``counterparts`` - computing it per row would be a query per
    template.
    """

    stages = serializers.SerializerMethodField()
    routes = WorkflowRoutePathReadSerializer(many=True, read_only=True)
    document_type_label = serializers.SerializerMethodField()
    is_platform = serializers.SerializerMethodField()
    tenant_has_own = serializers.SerializerMethodField()
    platform_updated_at = serializers.SerializerMethodField()
    platform_changed_since = serializers.SerializerMethodField()

    def get_document_type_label(self, obj) -> str:
        return document_type_label(obj.document_type)

    def get_stages(self, obj):
        from vs_workflow.services.positions import describe_tenant_positions

        active = list(
            obj.stages.filter(retired_at__isnull=True).order_by("order")
            .select_related("approver_role", "approver_group", "organogram_position",
                            "dynamic_role")
            .prefetch_related("dynamic_rules__role", "dynamic_role__rules__role",
                              "dynamic_role__rules__user", "dynamic_role__rules__group")
        )
        # Every post on the tenant's own chart the template names, in one lookup.
        labels = describe_tenant_positions(
            (obj.tenant_id, stage.organogram_tenant_position_id) for stage in active
        )
        return WorkflowStageReadSerializer(active, many=True, context={
            "position_labels": {pid: label for (_, pid), label in labels.items()},
        }).data

    def _counterpart(self, obj, which):
        pair = self.context.get("counterparts", {}).get((obj.document_type, obj.code))
        return (pair or {}).get(which)

    def get_is_platform(self, obj):
        """True when this is the shared definition rather than a tenant's own."""
        return obj.tenant_id is None

    def get_tenant_has_own(self, obj):
        """Only meaningful on a platform row: has this tenant adjusted it?"""
        if obj.tenant_id is not None:
            return None
        return self._counterpart(obj, "mine") is not None

    def get_platform_updated_at(self, obj):
        """When the shared version this one came from last changed."""
        if obj.tenant_id is None:
            return None
        platform = self._counterpart(obj, "platform")
        return platform.updated_at if platform else None

    def get_platform_changed_since(self, obj):
        """True when the shared version moved on after this tenant last saved.

        The signal a tenant needs: their own version is running, and the
        platform has since changed the one it was based on.
        """
        if obj.tenant_id is None:
            return False
        platform = self._counterpart(obj, "platform")
        return bool(platform and platform.updated_at > obj.updated_at)

    class Meta:
        model = WorkflowTemplate
        fields = [
            "id", "tenant", "branch", "document_type", "document_type_label", "code",
            "name", "description", "notification_events", "is_active",
            "is_platform", "tenant_has_own",
            "platform_updated_at", "platform_changed_since",
            "created_at", "updated_at", "stages", "routes",
        ]


class WorkflowTemplatePublishSerializer(serializers.Serializer):
    # PLATFORM publishes the shared definition every tenant starts on, and only
    # a platform actor may ask for it; the view enforces that. TENANT (the
    # default) writes the caller's own version.
    scope               = serializers.ChoiceField(
        choices=["TENANT", "PLATFORM"], required=False, default="TENANT")
    # The branch a TENANT template is for; see WorkflowTemplateViewSet.publish
    # for what leaving it out means. Resolved inside the tenant by the view.
    branch              = serializers.IntegerField(required=False, allow_null=True, min_value=1)
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
            # A ROLE stage must name the role it resolves; existence is checked
            # tenant-aware in the publish service (central stages cannot be).
            if s.get("approver_source", ApproverSource.ROLE.value) == ApproverSource.ROLE.value \
                    and not s.get("approver_role_key"):
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
            # A dynamic stage names its Dynamic Role, or carries rules of its
            # own. Both are checked tenant-aware in the publish service.
            if s.get("approver_source") == ApproverSource.DYNAMIC_ROLE.value:
                rules = s.get("dynamic_role_rules")
                if not s.get("dynamic_role_code") and (not rules or not isinstance(rules, list)):
                    raise serializers.ValidationError(
                        f"Stage '{label}': name the Dynamic Role this stage uses "
                        f"(dynamic_role_code) when approver_source is DYNAMIC_ROLE."
                    )
        return value


class _ProxyAttributionFields(serializers.Serializer):
    """The three "who did it" fields shared by votes and audit rows.

    ``real_actor_name`` and ``proxied_user_name`` are set only for an action
    taken under a proxy (an impersonation session); ``acted_label`` is always
    ready to display: "Ada Obi for Chioma Okafor" when proxied, otherwise the
    actor's own name. See :mod:`core.attribution`.
    """

    real_actor_name = serializers.SerializerMethodField()
    proxied_user_name = serializers.SerializerMethodField()
    acted_label = serializers.SerializerMethodField()

    def attribution(self, obj) -> dict:
        raise NotImplementedError

    def get_real_actor_name(self, obj) -> str | None:
        return self.attribution(obj)["real_actor_name"]

    def get_proxied_user_name(self, obj) -> str | None:
        return self.attribution(obj)["proxied_user_name"]

    def get_acted_label(self, obj) -> str:
        return self.attribution(obj)["acted_label"]


class WorkflowStageActionReadSerializer(_ProxyAttributionFields, serializers.ModelSerializer):
    """One vote or reversal. ``actor`` is the approver whose vote it is;
    ``proxied_by`` is the real person when the vote was cast under a proxy."""

    class Meta:
        model = WorkflowStageAction
        fields = [
            "id", "action", "actor", "on_behalf_of", "comment", "attempt",
            "acted_at", "reversed_at", "reversed_by", "reversal_reason", "is_reversal_of",
            "proxied_by", "real_actor_name", "proxied_user_name", "acted_label",
            "actor_is_exited", "on_behalf_of_is_exited", "proxied_by_is_exited",
            "reversed_by_is_exited",
        ]

    actor_is_exited = serializers.SerializerMethodField()
    on_behalf_of_is_exited = serializers.SerializerMethodField()
    proxied_by_is_exited = serializers.SerializerMethodField()
    reversed_by_is_exited = serializers.SerializerMethodField()

    def to_representation(self, obj):
        prime_exit_states(self.context, (
            obj.actor_id, obj.on_behalf_of_id, obj.proxied_by_id, obj.reversed_by_id,
        ))
        return super().to_representation(obj)

    def get_actor_is_exited(self, obj):
        return person_is_exited(self.context, obj.actor_id)

    def get_on_behalf_of_is_exited(self, obj):
        return person_is_exited(self.context, obj.on_behalf_of_id)

    def get_proxied_by_is_exited(self, obj):
        return person_is_exited(self.context, obj.proxied_by_id)

    def get_reversed_by_is_exited(self, obj):
        return person_is_exited(self.context, obj.reversed_by_id)

    def attribution(self, obj) -> dict:
        from core.attribution import vote_attribution

        return vote_attribution(obj)


class WorkflowStageApproverReadSerializer(serializers.ModelSerializer):
    user_is_exited = serializers.SerializerMethodField()
    on_behalf_of_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowStageApprover
        fields = ["id", "user", "on_behalf_of", "user_is_exited",
                  "on_behalf_of_is_exited", "attempt", "recorded_at"]

    def to_representation(self, obj):
        prime_exit_states(self.context, (obj.user_id, obj.on_behalf_of_id))
        return super().to_representation(obj)

    def get_user_is_exited(self, obj):
        return person_is_exited(self.context, obj.user_id)

    def get_on_behalf_of_is_exited(self, obj):
        return person_is_exited(self.context, obj.on_behalf_of_id)


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


class WorkflowAuditLogReadSerializer(_ProxyAttributionFields, serializers.ModelSerializer):
    """One engine event. ``actor`` is the person who really acted;
    ``effective_user`` is whom they acted as when the event was proxied."""

    class Meta:
        model = WorkflowAuditLog
        fields = ["id", "event_type", "actor", "stage_instance",
                  "context", "message", "occurred_at",
                  "effective_user", "real_actor_name", "proxied_user_name", "acted_label",
                  "actor_is_exited", "effective_user_is_exited"]

    actor_is_exited = serializers.SerializerMethodField()
    effective_user_is_exited = serializers.SerializerMethodField()

    def to_representation(self, obj):
        prime_exit_states(self.context, (obj.actor_id, obj.effective_user_id))
        return super().to_representation(obj)

    def get_actor_is_exited(self, obj):
        return person_is_exited(self.context, obj.actor_id)

    def get_effective_user_is_exited(self, obj):
        return person_is_exited(self.context, obj.effective_user_id)

    def attribution(self, obj) -> dict:
        from core.attribution import audit_row_attribution

        return audit_row_attribution(obj)


class WorkflowListSerializer(serializers.ListSerializer):
    """Fetch employment flags once for the people in an approval page."""

    def to_representation(self, data):
        rows = list(data)
        prime_exit_states(self.context, (row.requested_by_id for row in rows))
        return super().to_representation(rows)


class WorkflowInstanceListSerializer(serializers.ModelSerializer):
    """One instance as a queue row.

    ``document_type_label`` is what people call the document type, and
    ``document_title`` the title its handler gave this document when it was
    submitted ("JV-0042", "Finance Admin for Emeka Obi"), read from the stored
    summary so a page of rows costs no extra queries. Blank where the handler
    gave none.
    """

    template_code       = serializers.CharField(source="template.code",  read_only=True)
    current_stage_code  = serializers.CharField(source="current_stage.code",  read_only=True, default=None)
    current_stage_label = serializers.CharField(source="current_stage.label", read_only=True, default=None)
    document_type_label = serializers.SerializerMethodField()
    document_title      = serializers.SerializerMethodField()
    requested_by_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowInstance
        list_serializer_class = WorkflowListSerializer
        fields = [
            "id", "document_type", "document_type_label", "document_title",
            "document_object_id",
            "template_code",
            "status", "current_stage_code", "current_stage_label",
            "requested_by", "requested_by_is_exited", "submitted_at", "completed_at", "updated_at",
        ]

    def get_requested_by_is_exited(self, obj):
        return person_is_exited(self.context, obj.requested_by_id)

    def get_document_type_label(self, obj) -> str:
        return document_type_label(obj.document_type)

    def get_document_title(self, obj) -> str:
        summary = obj.document_summary if isinstance(obj.document_summary, dict) else {}
        title = summary.get("title")
        return title if isinstance(title, str) else ""


class WorkflowAdminListSerializer(serializers.ListSerializer):
    """Prime employment flags for an approval page before rendering its people."""

    def to_representation(self, data):
        rows = list(data)
        ids = set()
        for row in rows:
            ids.update((row.request_for_id, row.requested_by_id))
            for stage in getattr(row, "waiting_stage_instances", ()):
                for snap in stage.eligible_approvers.all():
                    ids.update((snap.user_id, snap.on_behalf_of_id))
        prime_exit_states(self.context, ids)
        return super().to_representation(rows)


class WorkflowInstanceAdminRowSerializer(WorkflowInstanceListSerializer):
    """One row of the administrators' list of every request, with who it waits on.

    Only ``GET /workflow/instances/`` uses it: the personal queues keep the
    plain row. Every value is read from what the view prefetched
    (``approval_stages`` on the template, ``waiting_stage_instances`` on the
    instance, with their approvers and live votes), so a page costs the same
    number of queries however many rows it holds.

    ``waiting_on`` lists everybody holding an undecided place on the active
    stage's current attempt, delegates included; it is empty, and
    ``waiting_since`` null, when the request is not waiting on a stage.
    ``stage_position`` is the current stage's place among the template's live
    approval stages, counted from 1.
    """

    request_for = serializers.SerializerMethodField()
    waiting_on = serializers.SerializerMethodField()
    waiting_since = serializers.SerializerMethodField()
    stage_position = serializers.SerializerMethodField()
    branch = serializers.SerializerMethodField()

    class Meta(WorkflowInstanceListSerializer.Meta):
        list_serializer_class = WorkflowAdminListSerializer
        fields = WorkflowInstanceListSerializer.Meta.fields + [
            "request_for", "waiting_on", "waiting_since", "stage_position", "branch",
        ]

    @staticmethod
    def _waiting_stage(obj):
        if obj.status != "IN_PROGRESS" or obj.current_stage_id is None:
            return None
        found = [si for si in getattr(obj, "waiting_stage_instances", [])
                 if si.stage_id == obj.current_stage_id]
        return max(found, key=lambda si: si.attempt) if found else None

    def get_request_for(self, obj):
        from vs_workflow.services.reassignment import person

        result = person(obj.request_for)
        if result is not None:
            result["is_exited"] = person_is_exited(self.context, obj.request_for_id)
        return result

    def get_waiting_on(self, obj):
        from vs_workflow.services.reassignment import person

        si = self._waiting_stage(obj)
        if si is None:
            return []
        decided = set()
        for action in si.live_actions:
            if action.attempt == si.attempt:
                decided.add(action.actor_id)
                if action.proxied_by_id:
                    decided.add(action.proxied_by_id)
        return [
            {**person(snap.user), "is_exited": person_is_exited(self.context, snap.user_id),
             "on_behalf_of": (
                 {**person(snap.on_behalf_of), "is_exited": person_is_exited(
                     self.context, snap.on_behalf_of_id)}
                 if snap.on_behalf_of_id else None)}
            for snap in si.eligible_approvers.all()
            if snap.attempt == si.attempt and snap.user_id not in decided
        ]

    def get_waiting_since(self, obj):
        si = self._waiting_stage(obj)
        if si is None or si.activated_at is None:
            return None
        return serializers.DateTimeField().to_representation(si.activated_at)

    def get_stage_position(self, obj):
        if obj.current_stage_id is None:
            return None
        stages = getattr(obj.template, "approval_stages", None)
        if stages is None:
            return None
        ids = [s.pk for s in stages]
        if obj.current_stage_id not in ids:
            return None
        return {"index": ids.index(obj.current_stage_id) + 1, "total": len(ids)}

    def get_branch(self, obj):
        if obj.branch_id is None:
            return None
        return {"id": obj.branch_id, "name": obj.branch.name}


class StageApproversWriteSerializer(serializers.Serializer):
    """``POST /workflow/instances/{id}/approvers/``: the complete list for one stage.

    The reason is checked by the service rather than here, so a missing or
    over-long one answers with its own code (``REASON_REQUIRED``).
    """

    stage = serializers.CharField(max_length=8)
    approvers = serializers.ListField(child=serializers.IntegerField(min_value=1),
                                      allow_empty=True, max_length=100)
    reason = serializers.CharField(required=False, allow_blank=True, default="",
                                   trim_whitespace=False)


class StageAssignmentResetSerializer(serializers.Serializer):
    """``POST /workflow/instances/{id}/approvers/reset/``."""

    stage = serializers.CharField(max_length=8)
    reason = serializers.CharField(required=False, allow_blank=True, default="",
                                   trim_whitespace=False)


class ReplaceApproverSerializer(serializers.Serializer):
    """``POST /workflow/instances/replace-approver/``: one person for another, on 1 to 200 requests."""

    from_user = serializers.IntegerField(min_value=1)
    to_user = serializers.IntegerField(min_value=1)
    instance_ids = serializers.ListField(child=serializers.CharField(max_length=8),
                                         min_length=1, max_length=200)
    reason = serializers.CharField(required=False, allow_blank=True, default="",
                                   trim_whitespace=False)


class WorkflowInstanceDetailSerializer(WorkflowInstanceListSerializer):
    stage_instances = WorkflowStageInstanceReadSerializer(many=True, read_only=True)
    audit_logs      = WorkflowAuditLogReadSerializer(many=True, read_only=True)
    document_summary = serializers.SerializerMethodField()
    document_details = serializers.SerializerMethodField()
    source_document_link = serializers.SerializerMethodField()
    next_stage      = serializers.SerializerMethodField()

    class Meta(WorkflowInstanceListSerializer.Meta):
        fields = WorkflowInstanceListSerializer.Meta.fields + [
            "document_summary", "document_details", "source_document_link",
            "next_stage", "stage_instances", "audit_logs",
        ]

    def to_representation(self, obj):
        ids = {obj.requested_by_id, obj.request_for_id}
        for stage in obj.stage_instances.all():
            for snap in stage.eligible_approvers.all():
                ids.update((snap.user_id, snap.on_behalf_of_id))
            for action in stage.actions.all():
                ids.update((action.actor_id, action.on_behalf_of_id,
                            action.proxied_by_id, action.reversed_by_id))
        for row in obj.audit_logs.all():
            ids.update((row.actor_id, row.effective_user_id))
        prime_exit_states(self.context, ids)
        return super().to_representation(obj)

    def get_next_stage(self, obj):
        from vs_workflow.services.routing import preview_next_approval_stage
        return preview_next_approval_stage(obj)

    def get_document_summary(self, obj):
        return self._document_summary(obj)

    def get_document_details(self, obj):
        """The snapshotted layout, filtered for whoever is reading it now.

        The snapshot is built when the document is submitted and refreshed by
        modules that permit pending corrections. What a given approver may see
        of it is decided here, so two
        approvers of the same batch can be shown different columns without the
        stored document differing. Its dates are stored ISO and written in the
        tenant's display format as they are read.
        """
        from vs_workflow.presentation import details_dates_for_reader, for_reader

        return details_dates_for_reader(
            for_reader(obj.document_details, self.context.get("request")),
            getattr(obj, "tenant", None), branch=getattr(obj, "branch_id", None),
        )

    def _document_summary(self, obj):
        """Return the object's summary, built at most once per object.

        ``document_summary`` and ``source_document_link`` both read it, and
        building it loads the source document and re-resolves its link. The
        cache is keyed on the object itself rather than its ``pk``: a
        serializer is handed whatever object the caller has, which may be
        unsaved or a stand-in with no ``pk`` at all, and two unsaved instances
        share ``pk=None`` and would otherwise receive each other's summary.
        """
        cached = getattr(self, "_document_summary_cache", None)
        if cached is not None and cached[0] is obj:
            return cached[1]
        summary = self._build_document_summary(obj)
        self._document_summary_cache = (obj, summary)
        return summary

    def _build_document_summary(self, obj):
        """Merge the submission snapshot with the source record's live link.

        The snapshot's dates are stored ISO and written in the tenant's display
        format here, as it is read.
        """
        from vs_workflow.exceptions import UnknownDocumentTypeError
        from vs_workflow.handlers import get_handler
        from vs_workflow.presentation import summary_for_reader

        summary = summary_for_reader(
            obj.document_summary, getattr(obj, "tenant", None),
            branch=getattr(obj, "branch_id", None),
        )
        document = obj.document
        if document is None:
            return summary
        try:
            link = get_handler(obj.document_type).get_source_document_link(document)
        except UnknownDocumentTypeError:
            return summary
        if link:
            summary["link"] = link
        return summary

    def get_source_document_link(self, obj):
        summary = self._document_summary(obj)
        link = summary.get("link") if isinstance(summary, dict) else None
        return link if isinstance(link, str) and link else None


class StageActionWriteSerializer(serializers.Serializer):
    action  = serializers.ChoiceField(choices=["APPROVED", "REJECTED", "RETURNED"])
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class CancelInstanceSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ReverseActionSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ApprovalDelegationSerializer(
    TenantScopedSerializerMixin, serializers.ModelSerializer,
):
    """Hand one user's approval authority to another for a period.

    ``delegate`` is client-supplied, so it is the one field here that can move
    authority, and it is resolved *inside* the delegation's tenant rather than
    globally: approval authority is tenant-bound, and a delegation is the only
    way a caller can nominate an approver by id. The view supplies that tenant
    (``get_serializer_context``), and it is the same tenant ``perform_create``
    pins on the row, so what was validated and what is stored cannot diverge.

    ``TenantScopedRelatedField`` is reused from ``vs_rbac`` rather than
    reimplemented, because the guarantee is not only "refuse the foreigner". A
    foreign id and an absent id come back with one shared message and one status,
    so this surface cannot be walked to learn which user ids exist in other
    tenants. A local check written to say "that user is in another tenant" would
    be the enumeration oracle that field exists to close.
    """

    delegate = TenantScopedRelatedField(
        queryset=get_user_model().objects.all(),
        tenant_lookup="tenant",
        not_found=USER_NOT_FOUND,
    )
    # Optional, and only on create: the caller is the delegator unless they
    # name somebody else, which the view allows only an administrator.
    delegator = TenantScopedRelatedField(
        queryset=get_user_model().objects.all(),
        tenant_lookup="tenant",
        not_found=USER_NOT_FOUND,
        required=False,
    )
    # Blank for a delegation that covers every document type.
    document_type_label = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    delegator_is_exited = serializers.SerializerMethodField()
    delegate_is_exited = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalDelegation
        list_serializer_class = WorkflowDelegationPeopleListSerializer
        fields = [
            "id", "delegator", "delegate", "starts_at", "ends_at",
            "delegator_is_exited", "delegate_is_exited",
            "document_type", "document_type_label", "exclusive", "reason",
            "created_at", "created_by", "revoked_at",
        ]
        read_only_fields = ["id", "created_at", "created_by", "revoked_at"]

    def get_document_type_label(self, obj) -> str:
        return document_type_label(obj.document_type)

    def get_created_by(self, obj):
        from vs_workflow.services.reassignment import person

        value = person(obj.created_by)
        if value is not None:
            value["is_exited"] = person_is_exited(self.context, obj.created_by_id)
        return value

    def to_representation(self, obj):
        prime_exit_states(self.context, (
            obj.delegator_id, obj.delegate_id, obj.created_by_id,
        ))
        return super().to_representation(obj)

    def get_delegator_is_exited(self, obj):
        return person_is_exited(self.context, obj.delegator_id)

    def get_delegate_is_exited(self, obj):
        return person_is_exited(self.context, obj.delegate_id)

    def validate(self, attrs):
        """Fallback tenancy check on the delegate, and a real document type.

        The delegate check is unreachable through the API - the field above already resolves inside the
        tenant - and kept as the backstop for a binding that could not reach one
        (the mixin refuses those outright, so this covers a future field-level
        change too). It raises the identical message the lookup does, so neither
        route reveals that an id exists somewhere else.
        """
        tenant = self._tenant()
        if tenant is None:
            raise serializers.ValidationError({"tenant": "Tenant context is required."})

        delegate = attrs.get("delegate") or getattr(self.instance, "delegate", None)
        if delegate is not None and getattr(delegate, "tenant_id", None) != tenant.pk:
            raise serializers.ValidationError({"delegate": USER_NOT_FOUND})

        # Whose approvals a delegation hands over is fixed once it exists.
        if self.instance is not None:
            attrs.pop("delegator", None)
        request = self.context.get("request")
        delegator = (attrs.get("delegator") or getattr(self.instance, "delegator", None)
                     or getattr(request, "user", None))
        if delegate is not None and delegator is not None and delegate.pk == delegator.pk:
            raise serializers.ValidationError({
                "delegate": "Choose somebody other than the person handing over their approvals.",
            })

        # Blank covers every type. Anything else must be a type this tenant
        # raises, because the engine matches it exactly and a typo would save a
        # delegation that never applies.
        document_type = (attrs.get("document_type") or "").strip()
        if "document_type" in attrs:
            attrs["document_type"] = document_type
        if document_type:
            from vs_workflow.handlers.registry import handlers_raised_by

            if document_type not in handlers_raised_by(tenant):
                raise serializers.ValidationError({
                    "document_type": "Choose a document type from the list, or leave it as all types.",
                })
        return attrs


class ApproverPreviewRequestSerializer(serializers.Serializer):
    """Validates an ad-hoc stage config + sample requester for the approver
    preview endpoint. Mirrors the WorkflowStage approver fields so a template
    builder can ask "who would approve?" without persisting anything."""

    requester = serializers.CharField(help_text="User id of the sample requester.")
    approver_source = serializers.ChoiceField(
        choices=ApproverSource.choices, default=ApproverSource.ROLE,
    )
    # ROLE config - a role *key*, resolved inside the requester's tenant.
    approver_role_key = serializers.CharField(required=False, allow_blank=True, default="")
    # ORGANOGRAM config
    organogram_target = serializers.ChoiceField(
        choices=OrganogramTarget.choices, required=False, allow_blank=True, default="",
    )
    organogram_levels = serializers.IntegerField(required=False, min_value=1, default=1)
    # A post's code, looked up on the chart the caller's tenant uses (as publish does).
    organogram_position_code = serializers.CharField(required=False, allow_blank=True, default="")
    approver_scope = serializers.ChoiceField(
        choices=ApproverScope.choices, required=False, default=ApproverScope.PLATFORM,
    )
    # WORKFLOW_GROUP config - an approver group *code*.
    approver_group_code = serializers.CharField(required=False, allow_blank=True, default="")
    # DYNAMIC_ROLE config: the rules to try, plus the sample document to try
    # them against, so a builder can check "a 150,000 request goes to the
    # Bursar" before publishing anything.
    dynamic_role_rules = serializers.ListField(
        child=serializers.DictField(), required=False, default=list)
    sample_document = serializers.DictField(required=False, default=dict)
    # Or a saved Dynamic Role, by code, tried against ``sample`` - the amount,
    # branch and document fields a real document would carry.
    dynamic_role_code = serializers.CharField(required=False, allow_blank=True, default="")
    sample = serializers.DictField(required=False, default=dict)
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
            if not attrs.get("dynamic_role_rules") and not attrs.get("dynamic_role_code"):
                raise serializers.ValidationError({"dynamic_role_rules": (
                    "Required when approver_source is DYNAMIC_ROLE, unless "
                    "dynamic_role_code names a saved Dynamic Role.")})
        elif not attrs.get("approver_role_key"):
            raise serializers.ValidationError(
                {"approver_role_key": "Required when approver_source is ROLE."})
        return attrs


# ── Approver groups (the "Workflow Approver" screen) ─────────────────────────

class WorkflowApproverGroupMemberReadSerializer(serializers.ModelSerializer):
    """One membership row, with the display fields the screen needs.

    Live resolution ("resolves to N people") is not computed here - it is
    served per group by the group detail/resolve endpoints, so listing many
    groups does not run one resolution query per member row.

    ``position_code`` and ``position_title`` describe whichever post a POSITION
    row names. Posts on a tenant's own chart are described in one lookup for
    everything the outermost serializer is rendering (a page of groups, or one
    group), cached on that serializer, so a page of groups costs the same one
    lookup however many members it holds.
    """

    role_key      = serializers.CharField(source="role.key",       read_only=True, default=None)
    role_name     = serializers.CharField(source="role.name",      read_only=True, default=None)
    position_code = serializers.SerializerMethodField()
    position_title = serializers.SerializerMethodField()
    user_name     = serializers.SerializerMethodField()
    user_is_exited = serializers.SerializerMethodField()
    user_email    = serializers.CharField(source="user.email",     read_only=True, default=None)

    class Meta:
        model = WorkflowApproverGroupMember
        list_serializer_class = WorkflowNamedUserListSerializer
        fields = [
            "id", "kind", "user", "user_name", "user_email", "user_is_exited",
            "role", "role_key", "role_name",
            "position", "position_code", "position_title", "added_at",
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        if obj.user is None:
            return None
        return getattr(obj.user, "full_name", "") or obj.user.get_username()

    def get_user_is_exited(self, obj):
        return person_is_exited(self.context, obj.user_id)

    def get_position_code(self, obj):
        label = self._position_label(obj)
        return label[0] if label else None

    def get_position_title(self, obj):
        label = self._position_label(obj)
        return label[1] if label else None

    def _position_label(self, obj):
        if obj.position_id:
            return obj.position.code, obj.position.title
        if obj.tenant_position_id is None:
            return None
        return self._tenant_position_labels().get((obj.group.tenant_id, obj.tenant_position_id))

    def _tenant_position_labels(self):
        """Every tenant-chart post the outermost serializer renders, described once."""
        root = self.root
        labels = getattr(root, "_tenant_position_labels", None)
        if labels is not None:
            return labels
        from vs_workflow.services.positions import describe_tenant_positions

        rendered = root.instance
        if isinstance(rendered, WorkflowApproverGroup):
            groups = [rendered]
        elif isinstance(rendered, WorkflowApproverGroupMember):
            groups = [rendered.group]
        else:
            groups = list(rendered or [])
        labels = describe_tenant_positions(
            (group.tenant_id, member.tenant_position_id)
            for group in groups for member in group.members.all()
            if member.tenant_position_id is not None
        )
        root._tenant_position_labels = labels
        return labels


class WorkflowApproverGroupSerializer(serializers.ModelSerializer):
    members = WorkflowApproverGroupMemberReadSerializer(many=True, read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowApproverGroup
        list_serializer_class = WorkflowContainerPeopleListSerializer
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
        if value is not None and tenant is not None and value.tenant_id != tenant.pk:
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
            # The group's tenant decides the chart, so a seat on the other
            # chart is refused like a code nobody has used.
            from vs_workflow.exceptions import UnknownPositionError
            from vs_workflow.services.positions import bind_position

            try:
                attrs["resolved_target"] = bind_position(attrs["position_code"], tenant)
            except UnknownPositionError as exc:
                raise serializers.ValidationError({"position_code": exc.message})
        return attrs


class WorkflowStageApproverOverrideSerializer(serializers.ModelSerializer):
    """A tenant's own approver for one stage of a template it did not author."""

    stage_code     = serializers.CharField(source="stage.code",  read_only=True)
    stage_label    = serializers.CharField(source="stage.label", read_only=True)
    template_code  = serializers.CharField(source="stage.template.code", read_only=True)
    document_type  = serializers.CharField(source="stage.template.document_type",
                                           read_only=True)
    is_central     = serializers.SerializerMethodField()
    approver_group_code = serializers.CharField(
        source="approver_group.code", read_only=True, default=None)

    class Meta:
        model = WorkflowStageApproverOverride
        fields = [
            "id", "stage", "stage_code", "stage_label", "template_code",
            "document_type", "is_central",
            "approver_source", "approver_role_key",
            "approver_group", "approver_group_code",
            "note", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_is_central(self, obj):
        return obj.stage.template.tenant_id is None

    def validate(self, attrs):
        tenant = self.context["tenant"]
        source = attrs.get("approver_source",
                           getattr(self.instance, "approver_source", None))
        role_key = attrs.get("approver_role_key",
                             getattr(self.instance, "approver_role_key", ""))
        group = attrs.get("approver_group",
                          getattr(self.instance, "approver_group", None))
        stage = attrs.get("stage", getattr(self.instance, "stage", None))

        # Only the two sources a tenant can meaningfully choose between. The
        # organogram and dynamic rules belong to whoever authored the template.
        if source not in (ApproverSource.ROLE, ApproverSource.WORKFLOW_GROUP):
            raise serializers.ValidationError({
                "approver_source": "An override may be ROLE or WORKFLOW_GROUP."})

        if source == ApproverSource.ROLE:
            if not role_key:
                raise serializers.ValidationError({
                    "approver_role_key": "Required when approver_source is ROLE."})
            from vs_rbac.models import TenantRoleTemplate
            if not TenantRoleTemplate.objects.filter(
                    tenant=tenant, key=role_key,
                    status=TenantRoleTemplate.Status.ACTIVE).exists():
                raise serializers.ValidationError({
                    "approver_role_key":
                        "No active role with that key exists in your tenant."})
            attrs["approver_group"] = None
        else:
            if group is None:
                raise serializers.ValidationError({
                    "approver_group": "Required when approver_source is WORKFLOW_GROUP."})
            if group.tenant_id != tenant.pk:
                raise serializers.ValidationError({
                    "approver_group": "That approver group belongs to another tenant."})
            attrs["approver_role_key"] = ""

        # A tenant may only repoint a stage it can actually reach: one on a
        # central template, or on a template it owns.
        if stage is not None:
            owner = stage.template.tenant_id
            if owner is not None and owner != tenant.pk:
                raise serializers.ValidationError({
                    "stage": "That stage belongs to another tenant's template."})
        return attrs


# ── Dynamic Roles (the Approvers screen's Dynamic Role tab) ──────────────────

class WorkflowDynamicRoleSerializer(serializers.ModelSerializer):
    """A named Dynamic Role, written together with all of its rules.

    ``rules`` is written as the whole ordered list and replaces what was there,
    because order is the contract - first match wins - and a partial edit could
    leave the Otherwise row anywhere. It is read back with each target's names.
    ``used_by`` lists the live stages routing through it, so the screen can say
    what an edit will reach.

    Changing ``document_types`` re-checks the stored rules against the fields
    the new types have, and is refused while a stage on a type being dropped
    still uses the Dynamic Role.
    """

    rules = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)
    used_by = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowDynamicRole
        list_serializer_class = WorkflowContainerPeopleListSerializer
        fields = ["id", "code", "name", "description", "document_types", "is_active",
                  "rules", "used_by", "created_at", "updated_at"]
        read_only_fields = ["id", "used_by", "created_at", "updated_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["rules"] = WorkflowDynamicRoleRuleReadSerializer(
            instance.rules.all(), many=True, context=self.context,
        ).data
        return data

    def get_used_by(self, obj):
        stages = obj.workflow_stages.filter(retired_at__isnull=True).select_related("template")
        return [
            {"template_id": s.template_id, "template_name": s.template.name,
             "document_type": s.template.document_type,
             "document_type_label": document_type_label(s.template.document_type),
             "stage_code": s.code, "stage_label": s.label}
            for s in stages
        ]

    def validate_code(self, value):
        """Codes are the stable handle templates publish against, so they stay
        unique per tenant and cannot change once a Dynamic Role exists."""
        tenant = self.context.get("tenant")
        if self.instance is not None:
            if value != self.instance.code:
                raise serializers.ValidationError(
                    "A Dynamic Role's code cannot be changed - templates reference it. "
                    "Create a new one instead.")
            return value
        if tenant is not None and WorkflowDynamicRole.all_objects.filter(
                tenant=tenant, code=value).exists():
            raise serializers.ValidationError(
                f"A Dynamic Role with code '{value}' already exists in this tenant.")
        return value

    def validate_document_types(self, value):
        """Types the tenant raises, each once - the check the fields endpoint makes."""
        from vs_workflow.exceptions import TemplateInvalidError
        from vs_workflow.services.dynamic_roles import check_document_types

        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise serializers.ValidationError("A list of document types.")
        try:
            return check_document_types(self.context.get("tenant"), value)
        except TemplateInvalidError as exc:
            raise serializers.ValidationError(exc.message)

    def validate(self, attrs):
        from vs_workflow.exceptions import TemplateInvalidError
        from vs_workflow.services.dynamic_roles import rules_as_payload, validate_rules

        tenant = self.context["tenant"]
        instance = self.instance
        types = attrs.get("document_types", getattr(instance, "document_types", None) or [])
        rules = attrs.pop("rules", None)
        if instance is None and rules is None:
            raise serializers.ValidationError(
                {"rules": "A Dynamic Role needs its rules, ending with the Otherwise rule."})
        if instance is not None and "document_types" in attrs:
            if types:
                for stage in (instance.workflow_stages.filter(retired_at__isnull=True)
                              .select_related("template")):
                    if stage.template.document_type not in types:
                        raise serializers.ValidationError({"document_types": (
                            f"'{stage.template.name}' ({stage.label}) still uses this "
                            "Dynamic Role for a document type you are removing. Point "
                            "that stage elsewhere first.")})
            if rules is None:
                rules = rules_as_payload(instance)
        if rules is not None:
            try:
                attrs["rule_specs"] = validate_rules(
                    tenant=tenant, document_types=types, rules=rules)
            except TemplateInvalidError as exc:
                raise serializers.ValidationError({"rules": exc.message})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        from vs_workflow.services.dynamic_roles import replace_rules

        specs = validated_data.pop("rule_specs")
        instance = super().create(validated_data)
        replace_rules(instance, specs)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        from vs_workflow.services.dynamic_roles import replace_rules

        specs = validated_data.pop("rule_specs", None)
        instance = super().update(instance, validated_data)
        if specs is not None:
            replace_rules(instance, specs)
        return instance


class DynamicRolePreviewSerializer(serializers.Serializer):
    """A trial run of unsaved Dynamic Role rules, for one requester and a sample.

    ``sample`` carries what a document would: ``amount`` in kobo, ``branch``,
    ``document_type``, and ``document`` - the type's own fields. ``branch``
    narrows role holders the way a BRANCH-scoped stage would.
    """

    requester = serializers.CharField(help_text="User id of the sample requester, in your tenant.")
    document_types = serializers.ListField(child=serializers.CharField(), required=False,
                                           default=list)
    rules = serializers.ListField(child=serializers.DictField())
    sample = serializers.DictField(required=False, default=dict)
    branch = serializers.CharField(required=False, allow_blank=True, default="")
