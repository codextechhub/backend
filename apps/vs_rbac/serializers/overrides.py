"""Per-user permission override serializers.

Read rows carry a computed ``granted_by_role`` flag so the UI can say
"the role grants this - denied for this user" (or, for a pre-emptive deny,
"the role doesn't grant this anyway") without a second round trip.

Scope is never taken from the request body: ``tenant``, ``user`` and
``created_by`` are injected by the view from the URL / authenticated actor.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from ..models import (
    Permission,
    PermissionScope,
    UserPermissionOverride,
    tenant_is_platform,
)


class UserPermissionOverrideSerializer(serializers.ModelSerializer):
    """One permission exception on one user."""

    permission = serializers.PrimaryKeyRelatedField(
        queryset=Permission.objects.filter(is_active=True),
        help_text="Dotted permission key, e.g. 'school.students.update'.",
    )
    permission_key = serializers.CharField(source="permission_id", read_only=True)
    permission_description = serializers.CharField(
        source="permission.description", read_only=True,
    )
    permission_sensitivity = serializers.CharField(
        source="permission.sensitivity_level", read_only=True,
    )

    user_id = serializers.SerializerMethodField()
    created_by_id = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    granted_by_role = serializers.SerializerMethodField()

    class Meta:
        model = UserPermissionOverride
        fields = [
            "id",
            "user_id",
            "permission",
            "permission_key",
            "permission_description",
            "permission_sensitivity",
            "mode",
            "reason",
            "expires_at",
            "is_expired",
            "granted_by_role",
            "created_by_id",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "user_id",
            "permission_key",
            "permission_description",
            "permission_sensitivity",
            "is_expired",
            "granted_by_role",
            "created_by_id",
            "created_by_name",
            "created_at",
            "updated_at",
        ]

    # ── computed ──────────────────────────────────────────────────────────────
    def get_user_id(self, obj) -> str | None:
        return str(obj.user_id) if obj.user_id else None

    def get_created_by_id(self, obj) -> str | None:
        return str(obj.created_by_id) if obj.created_by_id else None

    def get_created_by_name(self, obj) -> str | None:
        if not obj.created_by_id:
            return None
        return getattr(obj.created_by, "full_name", None) or getattr(
            obj.created_by, "email", None,
        )

    def get_is_expired(self, obj) -> bool:
        return obj.is_expired

    def get_granted_by_role(self, obj) -> bool:
        """Does any of the target's active roles currently grant this key?

        The view computes the target's role-only permission set once and puts
        it in the serializer context, so this stays free per row.
        """
        role_keys = self.context.get("role_permission_keys")
        if role_keys is None:
            return False
        return obj.permission_id in role_keys

    # ── validation ────────────────────────────────────────────────────────────
    def validate_reason(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A reason is required.")
        return value

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Expiry must be in the future.")
        return value

    def validate_mode(self, value: str) -> str:
        if value not in UserPermissionOverride.Mode.values:
            raise serializers.ValidationError("Mode must be ALLOW or DENY.")
        return value

    def validate(self, attrs):
        """Refuse an ALLOW of a key this tenant may not hold.

        The queryset above offers every active key, and it has to: a platform
        actor managing a CX colleague's overrides legitimately picks platform
        keys from it. What decides is the *tenant the override lands in*, which
        only the view knows, so the check belongs here rather than in the field.
        ``UserPermissionOverride.save()`` refuses it as well - this is the layer
        that makes the refusal a 400 with a field name on it.

        A DENY is never restricted: taking a key away cannot escalate anyone,
        and a school may want a pre-emptive deny on record.
        """
        attrs = super().validate(attrs)
        tenant = self.context.get("tenant")
        permission = attrs.get("permission")
        mode = attrs.get("mode")
        if (
            permission is None
            or mode != UserPermissionOverride.Mode.ALLOW
            or tenant is None
            or tenant_is_platform(tenant)
        ):
            return attrs
        if getattr(permission, "scope", "") != PermissionScope.TENANT:
            raise serializers.ValidationError({
                "permission": (
                    f"'{permission.pk}' is platform-scoped and cannot be granted "
                    f"to a user inside a tenant."
                ),
            })
        return attrs
