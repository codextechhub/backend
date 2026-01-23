from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from .models import (
    AdminActionLog,
    FeatureFlag,
    ImpersonationSession,
    ImportJobLog,
    ProvisioningEvent,
)

# -----------------------------------------------------------------------------
# Admin Action Log
# -----------------------------------------------------------------------------

class AdminActionLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True)
    
    class Meta:
        model = AdminActionLog
        fields = [
            "id",
            "created_at",
            "updated_at",
            "actor",
            "actor_email",
            "institution",
            "action",
            "result",
            "reason",
            "metadata",
            "error_message",
        ]
    read_only_fields = ["id", "created_at", "updated_at", "actor_email"]
    
class AdminActionLogCreateSerializer(serializers.ModelSerializer):
    """
    Use this when the Admin Console creates an action log entry.
    Tip: set actor in the view as request.user (don't accept actor from the client).
    """
    class Meta:
        model = AdminActionLog
        fields = [
            "institution",
            "action",
            "result",
            "reason",
            "metadata",
            "error_message",
        ]
    
    def validate(self, attrs):
        # Mirror the model's rule in a user-friendly way.
        action = attrs.get("action")
        reason = (attrs.get('reason')or "").strip()
        risky_actions = {"INSTITUTION_RESET", "IMPERSONATION_START"}
        if action in risky_actions and not reason:
            raise serializers.ValidationError("Reason is required for resets and impersonation.")
        return attrs
    
# -----------------------------------------------------------------------------
# Impersonation
# -----------------------------------------------------------------------------

class ImpersonationSessionSerializer(serializers.ModelSerializer):
    staff_email = serializers.EmailField(source="staff_user.email", read_only=True)
    target_email = serializers.EmailField(source="target_user.email", read_only=True)

    class Meta:
        model = ImpersonationSession
        fields = [
            "id",
            "created_at",
            "updated_at",
            "staff_user",
            "staff_email",
            "institution",
            "target_user",
            "target_email",
            "justification",
            "status",
            "started_at",
            "ends_at",
            "ended_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "staff_email", "target_email", "ended_at"]
        
class ImpersonationStartSerializer(serializers.Serializer):
    """
    Simple payload for starting impersonation.
    In your view/service, you'll create an ImpersonationSession object.
    """
    institution = serializers.IntegerField()
    target_user = serializers.IntegerField()
    justification = serializers.CharField()
    duration_minutes = serializers.IntegerField(min_value=5, max_value=240, default=30) # 5 min to 4 hours
    
    def validation_justification(self, value):
        if not value.strip():
            raise serializers.ValidationError("justification is required.")
        return value
    
class ImpersonationEndSerializer(serializers.Serializer):
    """
    Payload to end an impersonation session.
    """
    session_id = serializers.IntegerField()
    
    def validate_session_id(self, value):
        if value <= 0:
            raise serializers.ValidationError("Invalid session_id.")
        return value
        
# -----------------------------------------------------------------------------
# Feature Flags
# -----------------------------------------------------------------------------

class FeatureFlagSerializer(serializers.ModelSerializer):
    updated_by_email = serializers.EmailField(source="updated_by.email", read_only=True)

    class Meta:
        model = FeatureFlag
        fields = [
            "id",
            "created_at",
            "updated_at",
            "institution",
            "key",
            "enabled",
            "updated_by",
            "updated_by_email",
            "reason",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "updated_by_email"]
        
class FeatureFlagUpsertSerializer(serializers.Serializer):
    """
    Upsert payload:
    - If flag exists for (institution, key), update it
    - else create it
    """
    institution = serializers.IntegerField()
    key = serializers.CharField(max_length=120)
    enabled = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True, required=False)

    def validate_key(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("key is required.")
        return value
    
# -----------------------------------------------------------------------------
# Provisioning Events
# -----------------------------------------------------------------------------

class ProvisioningEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProvisioningEvent
        fields = [
            "id",
            "created_at",
            "updated_at",
            "institution",
            "step",
            "status",
            "message",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        
class ProvisioningRetrySerializer(serializers.Serializer):
    """
    Payload to retry a failed provisioning step.
    Your view should enforce rules like: cannot retry if step is RUNNING.
    """
    institution = serializers.IntegerField()
    step = serializers.CharField(max_length=120)
    reason = serializers.CharField()

    def validate(self, attrs):
        if not attrs["reason"].strip():
            raise serializers.ValidationError({"reason": "Reason is required to retry a step."})
        return attrs

# -----------------------------------------------------------------------------
# Import Jobs (Admin Console tracking)
# -----------------------------------------------------------------------------

class ImportJobLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportJobLog
        fields = [
            "id",
            "created_at",
            "updated_at",
            "institution",
            "job_type",
            "status",
            "total_rows",
            "failed_rows",
            "error_report",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        
class ImportRetrySerializer(serializers.Serializer):
    """
    Payload to retry an import job.
    Use job_type if you don't have a separate import-job ID yet.
    """
    institution = serializers.IntegerField()
    job_type = serializers.CharField(max_length=120)
    reason = serializers.CharField()

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("Reason is required.")
        return value
    
# -----------------------------------------------------------------------------
# Dashboard helpers (nice for list endpoints)
# -----------------------------------------------------------------------------

class InstitutionDashboardItemSerializer(serializers.Serializer):
    """
    This is NOT a DB model serializer.
    It's a simple response shape for the Admin dashboard list.

    Your view can build this from:
      - Institution (Module 1)
      - latest ProvisioningEvent
      - latest ImportJobLog summaries
      - flags/suspension state (from Institution model)
    """
    institution_id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.CharField()

    lifecycle_state = serializers.CharField()
    provisioning_status = serializers.CharField(allow_blank=True, required=False)
    last_error = serializers.CharField(allow_blank=True, required=False)

    is_suspended = serializers.BooleanField()
    updated_at = serializers.DateTimeField()
    
class DashboardFilterSerializer(serializers.Serializer):
    """
    Parse query params for dashboard filtering.
    Use this in your view: serializer = DashboardFilterSerializer(data=request.query_params)
    """
    q = serializers.CharField(required=False, allow_blank=True)
    lifecycle_state = serializers.CharField(required=False, allow_blank=True)
    provisioning_status = serializers.CharField(required=False, allow_blank=True)
    is_suspended = serializers.BooleanField(required=False)

    created_after = serializers.DateTimeField(required=False)
    created_before = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        after = attrs.get("created_after")
        before = attrs.get("created_before")
        if after and before and after > before:
            raise serializers.ValidationError("created_after cannot be after created_before.")
        return attrs

# -----------------------------------------------------------------------------
# Small convenience: readable time fields (optional)
# -----------------------------------------------------------------------------

class HumanTimeMixin(serializers.Serializer):
    """
    If you ever want to add 'created_ago' fields later, you can reuse this.
    Keeping it here optional and harmless.
    """
    created_ago = serializers.SerializerMethodField()

    def get_created_ago(self, obj):
        if not getattr(obj, "created_at", None):
            return ""
        delta = timezone.now() - obj.created_at
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"