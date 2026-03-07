from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from vs_institutions.models import Institution
from .models import (
    AuditEvent,
    EntityAuditTrail,
    AuditExportJob,
    ComplianceRule,
    AuditSeverity,
    AuditActorType,
    AuditStatus,
    AuditModuleKey,
    AuditActionType,
    ExportFormat,
    ExportJobStatus,
    ComplianceRuleType,
)

User = get_user_model()


# -----------------------------------------------------------------------------
# Small reusable serializers
# -----------------------------------------------------------------------------

class InstitutionSlimSerializer(serializers.ModelSerializer):
    """
    Small institution serializer.
    Use this when you only want basic institution details inside another response.
    """

    class Meta:
        model = Institution
        fields = ("id", "name", "slug")


class UserSlimSerializer(serializers.ModelSerializer):
    """
    Small user serializer.
    Adjust fields if your User model uses different names.
    """

    class Meta:
        model = User
        fields = ("id", "email")


# -----------------------------------------------------------------------------
# Audit Event Serializers
# -----------------------------------------------------------------------------

class AuditEventListSerializer(serializers.ModelSerializer):
    """
    Use this for audit log listing pages.

    Why it exists:
    - list pages usually need lighter data
    - we avoid dumping all JSON snapshots on every row
    """

    institution = InstitutionSlimSerializer(read_only=True)
    actor_user = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id",
            "institution",
            "module_key",
            "action_type",
            "severity",
            "status",
            "actor_type",
            "actor_user",
            "actor_label",
            "entity_type",
            "entity_id",
            "entity_label",
            "summary",
            "event_at",
        )


class AuditEventDetailSerializer(serializers.ModelSerializer):
    """
    Use this for opening a single audit event in detail view.

    This includes snapshots and metadata.
    """

    institution = InstitutionSlimSerializer(read_only=True)
    actor_user = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id",
            "institution",
            "module_key",
            "action_type",
            "severity",
            "status",
            "actor_type",
            "actor_user",
            "actor_label",
            "entity_type",
            "entity_id",
            "entity_label",
            "summary",
            "before_data",
            "after_data",
            "metadata",
            "event_at",
            "created_at",
            "updated_at",
        )


class AuditEventCreateSerializer(serializers.ModelSerializer):
    """
    Use this when creating a new audit event manually or from a service.

    Notes:
    - actor_user is optional because system/service actions may not have a real user
    - institution is optional because some events are global/platform-level
    """

    class Meta:
        model = AuditEvent
        fields = (
            "institution",
            "module_key",
            "action_type",
            "severity",
            "status",
            "actor_type",
            "actor_user",
            "actor_label",
            "entity_type",
            "entity_id",
            "entity_label",
            "summary",
            "before_data",
            "after_data",
            "metadata",
            "event_at",
        )

    def validate(self, attrs):
        """
        Cross-field validation.

        This is where we check relationships between fields,
        not just one field at a time.
        """

        actor_type = attrs.get("actor_type")
        actor_user = attrs.get("actor_user")
        actor_label = attrs.get("actor_label")
        entity_type = attrs.get("entity_type")
        entity_id = attrs.get("entity_id")
        summary = attrs.get("summary")

        if actor_type == AuditActorType.USER and not actor_user and not actor_label:
            raise serializers.ValidationError(
                {"actor_user": "User actor type requires actor_user or actor_label."}
            )

        if not entity_type:
            raise serializers.ValidationError(
                {"entity_type": "This field is required."}
            )

        if not entity_id:
            raise serializers.ValidationError(
                {"entity_id": "This field is required."}
            )

        if not summary:
            raise serializers.ValidationError(
                {"summary": "This field is required."}
            )

        return attrs

    def create(self, validated_data):
        """
        Create a new audit event.

        Since the model itself is immutable after creation,
        this serializer is only for creating rows, not updating them.
        """
        return AuditEvent.objects.create(**validated_data)


# -----------------------------------------------------------------------------
# Entity Audit Trail Serializers
# -----------------------------------------------------------------------------

class EntityAuditTrailSerializer(serializers.ModelSerializer):
    """
    Serializer for the summary trail table/model.
    """

    institution = InstitutionSlimSerializer(read_only=True)

    class Meta:
        model = EntityAuditTrail
        fields = (
            "id",
            "institution",
            "entity_type",
            "entity_id",
            "entity_label",
            "event_count",
            "first_event_at",
            "last_event_at",
            "created_at",
            "updated_at",
        )


class EntityAuditTrailDetailSerializer(serializers.Serializer):
    """
    This is NOT tied directly to one model row.

    Why?
    Because a full entity trail page often needs:
    - the trail summary
    - the actual list of event rows

    So this serializer groups both together.
    """

    trail = EntityAuditTrailSerializer()
    events = AuditEventListSerializer(many=True)


# -----------------------------------------------------------------------------
# Audit Export Job Serializers
# -----------------------------------------------------------------------------

class AuditExportJobListSerializer(serializers.ModelSerializer):
    """
    Lighter serializer for export history listing.
    """

    institution = InstitutionSlimSerializer(read_only=True)
    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
            "institution",
            "requested_by",
            "export_format",
            "status",
            "file_name",
            "row_count",
            "requested_at",
            "started_at",
            "completed_at",
            "expires_at",
        )


class AuditExportJobDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one export job.
    """

    institution = InstitutionSlimSerializer(read_only=True)
    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
            "institution",
            "requested_by",
            "export_format",
            "status",
            "filter_payload",
            "file_name",
            "file_path",
            "row_count",
            "failure_reason",
            "requested_at",
            "started_at",
            "completed_at",
            "expires_at",
            "created_at",
            "updated_at",
        )


class AuditExportJobCreateSerializer(serializers.ModelSerializer):
    """
    Use this when a user requests a CSV export.

    Usually:
    - requested_by should come from request.user in the view
    - institution may come from current institution context
    """

    class Meta:
        model = AuditExportJob
        fields = (
            "institution",
            "export_format",
            "filter_payload",
        )

    def validate_export_format(self, value):
        if value != ExportFormat.CSV:
            raise serializers.ValidationError("Only CSV export is currently supported.")
        return value

    def create(self, validated_data):
        request = self.context.get("request")
        user = getattr(request, "user", None)

        return AuditExportJob.objects.create(
            requested_by=user if user and user.is_authenticated else None,
            **validated_data,
        )


# -----------------------------------------------------------------------------
# Compliance Rule Serializers
# -----------------------------------------------------------------------------

class ComplianceRuleListSerializer(serializers.ModelSerializer):
    """
    Lighter serializer for listing compliance rules.
    """

    institution = InstitutionSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "rule_type",
            "institution",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
        )


class ComplianceRuleDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one compliance rule.
    """

    institution = InstitutionSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "description",
            "rule_type",
            "institution",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
            "masking_fields",
            "config",
            "created_at",
            "updated_at",
        )


class ComplianceRuleCreateUpdateSerializer(serializers.ModelSerializer):
    """
    Use this for creating and updating compliance rules.
    """

    class Meta:
        model = ComplianceRule
        fields = (
            "name",
            "description",
            "rule_type",
            "institution",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
            "masking_fields",
            "config",
        )

    def validate(self, attrs):
        rule_type = attrs.get("rule_type")
        retention_days = attrs.get("retention_days")

        if rule_type == ComplianceRuleType.RETENTION and not retention_days:
            raise serializers.ValidationError(
                {"retention_days": "Retention rules require retention_days."}
            )

        return attrs


# -----------------------------------------------------------------------------
# Search / Filter Serializer
# -----------------------------------------------------------------------------

class AuditEventFilterSerializer(serializers.Serializer):
    """
    This serializer is not for saving to the database.

    It is only for validating incoming filter/search query data.

    Typical use:
    - validate query params
    - then apply them in the view/queryset
    """

    institution_id = serializers.UUIDField(required=False)
    module_key = serializers.ChoiceField(
        choices=AuditModuleKey.choices,
        required=False,
    )
    action_type = serializers.ChoiceField(
        choices=AuditActionType.choices,
        required=False,
    )
    severity = serializers.ChoiceField(
        choices=AuditSeverity.choices,
        required=False,
    )
    status = serializers.ChoiceField(
        choices=AuditStatus.choices,
        required=False,
    )
    actor_type = serializers.ChoiceField(
        choices=AuditActorType.choices,
        required=False,
    )
    actor_user_id = serializers.UUIDField(required=False)
    entity_type = serializers.CharField(required=False)
    entity_id = serializers.CharField(required=False)

    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)

    search = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        """
        Validate date range.
        """

        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")

        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError(
                {"date_to": "date_to must be greater than or equal to date_from."}
            )

        return attrs