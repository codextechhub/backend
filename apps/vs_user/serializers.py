from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from rest_framework import serializers

from vs_institutions.models import Institution
from .models import (
    UserAccount,
    TemporaryPasswordIssue,
    LoginSession,
    RevokedToken,
    AuthAttempt,
    AccountLockout,
    PasswordResetRequest,
    SuspiciousLoginEvent,
    AuthEventLog,
)


# -----------------------------------------------------------------------------
# Small reusable helpers
# -----------------------------------------------------------------------------

class InstitutionSlimSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = ("id", "name", "slug")  # adjust if your Institution fields differ


def _raise_password_error(exc: DjangoValidationError):
    """
    Turn Django password validation errors into DRF-friendly messages.
    """
    if hasattr(exc, "messages"):
        raise serializers.ValidationError({"new_password": exc.messages})
    raise serializers.ValidationError({"new_password": ["Invalid password."]})


# -----------------------------------------------------------------------------
# USER ACCOUNT (FR-IDA-001, FR-IDA-002, FR-IDA-012)
# -----------------------------------------------------------------------------

class UserAccountReadSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for returning user info safely.
    """
    institution = InstitutionSlimSerializer(read_only=True)

    class Meta:
        model = UserAccount
        fields = (
            "id",
            "login_id",
            "institution",
            "email",
            "user_type",
            "status",
            "full_name",
            "phone",
            "must_change_password",
            "password_changed_at",
            "last_login_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class UserAccountCreateSerializer(serializers.ModelSerializer):
    """
    Create user accounts in a simple way (FR-IDA-001).

    IMPORTANT: This serializer does NOT email the temp password.
    Your view/service should:
      1) create user with a generated temp password
      2) create TemporaryPasswordIssue record
      3) send the password via chosen channel
      4) set must_change_password=True
      5) emit AuthEventLog / audit event
    """
    institution_id = serializers.PrimaryKeyRelatedField(
        source="institution",
        queryset=Institution.objects.all(),
        required=False,
        allow_null=True,
        help_text="Required for institution users; must be null for Vision staff.",
    )

    temp_password_channel = serializers.ChoiceField(
        choices=TemporaryPasswordIssue.Channel.choices,
        write_only=True,
        required=False,
        help_text="USER_EMAIL or SUPERVISOR_EMAIL (FR-IDA-002).",
    )
    supervisor_email = serializers.EmailField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Required only if temp_password_channel=SUPERVISOR_EMAIL.",
    )

    class Meta:
        model = UserAccount
        fields = (
            "institution_id",
            "email",
            "user_type",
            "status",
            "full_name",
            "phone",
            "temp_password_channel",
            "supervisor_email",
        )

    def validate(self, attrs):
        user_type = attrs.get("user_type")
        institution = attrs.get("institution")

        # Enforce your model rule in serializer too (nice UX)
        if user_type == UserAccount.UserType.VISION_STAFF:
            if institution is not None:
                raise serializers.ValidationError(
                    {"institution_id": "Vision staff users cannot be associated with an institution."}
                )
        else:
            if institution is None:
                raise serializers.ValidationError(
                    {"institution_id": "Non-Vision staff users must be associated with an institution."}
                )

        # Temp password channel rules (FR-IDA-002)
        ch = attrs.get("temp_password_channel")
        sup = attrs.get("supervisor_email", "")
        if ch == TemporaryPasswordIssue.Channel.SUPERVISOR_EMAIL and not sup:
            raise serializers.ValidationError(
                {"supervisor_email": "Supervisor email is required for SUPERVISOR_EMAIL delivery."}
            )

        return attrs

    def create(self, validated_data):
        # Remove serializer-only fields
        validated_data.pop("temp_password_channel", None)
        validated_data.pop("supervisor_email", None)

        # This serializer creates the account only.
        # Your service/view should handle temp password generation + delivery + TemporaryPasswordIssue.
        try:
            with transaction.atomic():
                user = UserAccount.objects.create(**validated_data)
                return user
        except IntegrityError:
            # Most common: email uniqueness constraints (case-insensitive per institution / staff)
            raise serializers.ValidationError(
                {"email": "This email is already used under the applicable uniqueness policy."}
            )


class UserAccountUpdateSerializer(serializers.ModelSerializer):
    """
    Simple partial update for profile-ish fields.
    Avoid editing status/user_type here unless your policy allows it.
    """
    class Meta:
        model = UserAccount
        fields = ("full_name", "phone", "status")
        extra_kwargs = {
            "status": {"required": False},
        }


# -----------------------------------------------------------------------------
# LOGIN / TOKEN (FR-IDA-003, FR-IDA-004, FR-IDA-005, FR-IDA-012)
# -----------------------------------------------------------------------------

class LoginRequestSerializer(serializers.Serializer):
    """
    Login payload (email + password + institution context).
    This matches your FRD's "institution-aware login enforcement" (FR-IDA-012).

    The view/service should:
      - resolve institution from context (slug/subdomain) or explicit field
      - verify credentials
      - create session
      - issue JWT tokens
      - enforce must_change_password if needed
    """
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    institution_slug = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Explicit institution context (subdomain/slug). Required for institution users.",
    )
    device_label = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        email = attrs.get("email", "").strip()
        if not email:
            raise serializers.ValidationError({"email": "Email is required."})
        return attrs


class TokenRefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True, help_text="Refresh token")


class TokenRevokeSerializer(serializers.Serializer):
    """
    Revoke token by JTI (or by token, depending on your implementation).
    Keep this simple.
    """
    jti = serializers.CharField()
    token_type = serializers.ChoiceField(choices=[("access", "access"), ("refresh", "refresh")])
    reason = serializers.CharField(required=False, allow_blank=True)


# -----------------------------------------------------------------------------
# PASSWORD CHANGE / RESET (FR-IDA-002, FR-IDA-011)
# -----------------------------------------------------------------------------

class PasswordChangeSerializer(serializers.Serializer):
    """
    Used when user is logged in and must change password (FR-IDA-002).
    """
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = self.context["request"].user

        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "Current password is incorrect."})

        # Validate password strength using Django validators
        try:
            validate_password(attrs["new_password"], user=user)
        except DjangoValidationError as exc:
            _raise_password_error(exc)

        # Block re-using current password
        if attrs["new_password"] == attrs["current_password"]:
            raise serializers.ValidationError({"new_password": "New password must be different."})

        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    """
    Request a password reset (FR-IDA-011).
    Always return a generic success from the view to avoid user enumeration.
    """
    email = serializers.EmailField()
    institution_slug = serializers.CharField(required=False, allow_blank=True)


class PasswordResetConfirmSerializer(serializers.Serializer):
    """
    Confirm reset with raw token + new password.
    Your service should:
      - hash token (PasswordResetRequest.hash_token)
      - find active reset request
      - set new password
      - mark used
    """
    token = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        try:
            validate_password(attrs["new_password"])
        except DjangoValidationError as exc:
            _raise_password_error(exc)
        return attrs


# -----------------------------------------------------------------------------
# TEMP PASSWORD ISSUANCE (FR-IDA-002)
# -----------------------------------------------------------------------------

class TemporaryPasswordIssueReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = TemporaryPasswordIssue
        fields = (
            "id",
            "user",
            "channel",
            "delivered_to",
            "expires_at",
            "delivered_at",
            "delivery_status",
            "failure_reason",
            "created_at",
        )
        read_only_fields = fields


class TemporaryPasswordIssueCreateSerializer(serializers.Serializer):
    """
    A small serializer for "regenerate/resend temp password" operations.

    The actual generation & sending should happen in a service.
    Serializer just validates input and returns normalized intent.
    """
    user_id = serializers.PrimaryKeyRelatedField(queryset=UserAccount.objects.all(), source="user")
    channel = serializers.ChoiceField(choices=TemporaryPasswordIssue.Channel.choices)
    delivered_to = serializers.EmailField()
    ttl_minutes = serializers.IntegerField(min_value=5, max_value=1440, default=60)

    def validate(self, attrs):
        user: UserAccount = attrs["user"]

        # Only allow for invited/active users per your policy (adjust as needed)
        if user.status in (UserAccount.Status.DELETED,):
            raise serializers.ValidationError("Cannot issue temp password for deleted users.")

        # For supervisor channel, delivered_to should not equal the user's email (optional rule)
        if attrs["channel"] == TemporaryPasswordIssue.Channel.SUPERVISOR_EMAIL and attrs["delivered_to"].lower() == user.email.lower():
            raise serializers.ValidationError({"delivered_to": "Supervisor email should differ from user email."})

        return attrs


# -----------------------------------------------------------------------------
# SESSIONS (FR-IDA-009, FR-IDA-010)
# -----------------------------------------------------------------------------

class LoginSessionReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoginSession
        fields = (
            "id",
            "user",
            "institution",
            "ip_address",
            "user_agent",
            "device_label",
            "last_seen_at",
            "is_active",
            "ended_at",
            "end_reason",
            "created_at",
        )
        read_only_fields = fields


class ForceLogoutSerializer(serializers.Serializer):
    """
    Force logout can be targeted at a session or user.
    Your service should revoke tokens + end sessions (FR-IDA-010).
    """
    user_id = serializers.PrimaryKeyRelatedField(queryset=UserAccount.objects.all(), required=False)
    session_id = serializers.PrimaryKeyRelatedField(queryset=LoginSession.objects.all(), required=False)
    reason = serializers.CharField()

    def validate(self, attrs):
        if not attrs.get("user_id") and not attrs.get("session_id"):
            raise serializers.ValidationError("Provide either user_id or session_id.")
        return attrs


# -----------------------------------------------------------------------------
# BRUTE FORCE / LOCKOUT (FR-IDA-006, FR-IDA-007, FR-IDA-008)
# -----------------------------------------------------------------------------

class AuthAttemptReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthAttempt
        fields = (
            "id",
            "email_entered",
            "institution_context",
            "user",
            "institution",
            "ip_address",
            "result",
            "failure_code",
            "metadata",
            "created_at",
        )
        read_only_fields = fields


class AccountLockoutReadSerializer(serializers.ModelSerializer):
    is_locked_now = serializers.SerializerMethodField()

    class Meta:
        model = AccountLockout
        fields = (
            "user",
            "locked_until",
            "locked_reason",
            "failure_count",
            "last_failure_at",
            "last_failure_ip",
            "is_locked_now",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_is_locked_now(self, obj: AccountLockout) -> bool:
        return obj.is_locked_now()


class UnlockAccountSerializer(serializers.Serializer):
    """
    Admin unlock request (FR-IDA-008).
    Your service may also force password reset (policy).
    """
    user_id = serializers.PrimaryKeyRelatedField(queryset=UserAccount.objects.all(), source="user")
    reason = serializers.CharField(required=False, allow_blank=True)
    force_password_reset = serializers.BooleanField(default=True)


# -----------------------------------------------------------------------------
# TOKEN REVOCATION STORE (FR-IDA-005)
# -----------------------------------------------------------------------------

class RevokedTokenReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = RevokedToken
        fields = (
            "id",
            "user",
            "jti",
            "token_type",
            "expires_at",
            "session",
            "reason",
            "revoked_by",
            "created_at",
        )
        read_only_fields = fields


# -----------------------------------------------------------------------------
# SUSPICIOUS EVENTS (FR-IDA-012, FR-IDA-006 optional signals)
# -----------------------------------------------------------------------------

class SuspiciousLoginEventReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = SuspiciousLoginEvent
        fields = (
            "id",
            "user",
            "email_entered",
            "institution_context",
            "ip_address",
            "event_type",
            "risk_score",
            "decision",
            "details",
            "created_at",
        )
        read_only_fields = fields


# -----------------------------------------------------------------------------
# AUTH EVENT LOG (FR-IDA-013)
# -----------------------------------------------------------------------------

class AuthEventLogReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthEventLog
        fields = (
            "id",
            "actor",
            "subject",
            "institution",
            "event",
            "ip_address",
            "user_agent",
            "metadata",
            "created_at",
        )
        read_only_fields = fields
