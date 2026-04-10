from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from .models import (
    ImpersonationSession,
)


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
            "school",
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
    school = serializers.IntegerField()
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
# Dashboard helpers (nice for list endpoints)
# -----------------------------------------------------------------------------

class SchoolDashboardItemSerializer(serializers.Serializer):
    """
    This is NOT a DB model serializer.
    It's a simple response shape for the Admin dashboard list.

    Your view can build this from:
      - School (Module 1)
      - latest ProvisioningEvent
      - latest ImportJobLog summaries
      - flags/suspension state (from School model)
    """
    school_id = serializers.IntegerField()
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