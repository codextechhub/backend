from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from vs_schools.models import School
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

class SchoolSlimSerializer(serializers.ModelSerializer):
    """
    Small school serializer.
    Use this when you only want basic school details inside another response.
    """

    class Meta:
        model = School
        fields = ("id", "name", "slug")


class UserSlimSerializer(serializers.ModelSerializer):
    """
    Small user serializer.
    Adjust fields if your User model uses different names.
    """
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "full_name")


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

    actor_user = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id",
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

    actor_user = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id",
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
            "diff_data",
            "metadata",
            "event_at",
        )


# -----------------------------------------------------------------------------
# Entity Audit Trail Serializers
# -----------------------------------------------------------------------------

class EntityAuditTrailSerializer(serializers.ModelSerializer):
    """
    Serializer for the summary trail table/model.
    """

    class Meta:
        model = EntityAuditTrail
        fields = (
            "id",
            "entity_type",
            "entity_id",
            "entity_label",
            "event_count",
            "first_event_at",
            "last_event_at",
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

    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
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

    requested_by = UserSlimSerializer(read_only=True)

    class Meta:
        model = AuditExportJob
        fields = (
            "id",
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
        )


# -----------------------------------------------------------------------------
# Compliance Rule Serializers
# -----------------------------------------------------------------------------

class ComplianceRuleListSerializer(serializers.ModelSerializer):
    """
    Lighter serializer for listing compliance rules.
    """

    school = SchoolSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "rule_type",
            "school",
            "module_key",
            "action_type",
            "is_active",
            "retention_days",
        )


class ComplianceRuleDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer for one compliance rule.
    """

    school = SchoolSlimSerializer(read_only=True)

    class Meta:
        model = ComplianceRule
        fields = (
            "id",
            "name",
            "description",
            "rule_type",
            "school",
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
            "school",
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
    actor_user_id = serializers.IntegerField(required=False)
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