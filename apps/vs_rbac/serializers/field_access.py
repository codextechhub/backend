"""Field Access serializers: a role's switch changes and one-person exceptions.

Scope never comes from the request body. The role, the tenant, the target user
and the actor are resolved by the view from the URL and the authenticated
request, and the view decides which fields the tenant may name.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from ..models import (
    FieldDefinition,
    PermissionScope,
    UserFieldAccessOverride,
    tenant_is_platform,
)

#: The most changes one PATCH may carry.
MAX_FIELD_ACCESS_CHANGES = 200


class FieldAccessChangeSerializer(serializers.Serializer):
    """One entry of a role's field access PATCH.

    Either a switch change (``read`` and/or ``write``) or ``reset: true``,
    never both. ``read: false`` with ``write: true`` is refused because the
    two normalisation rules (Write implies Read, Read off forces Write off)
    point opposite ways and neither can be applied without guessing.
    """

    field = serializers.CharField(max_length=200)
    read = serializers.BooleanField(required=False)
    write = serializers.BooleanField(required=False)
    reset = serializers.BooleanField(required=False)

    def validate(self, attrs):
        has_switch = "read" in attrs or "write" in attrs
        if attrs.get("reset") and has_switch:
            raise serializers.ValidationError(
                "A reset cannot also set read or write."
            )
        if not attrs.get("reset") and not has_switch:
            raise serializers.ValidationError(
                "Send read, write, or reset: true."
            )
        if attrs.get("read") is False and attrs.get("write") is True:
            raise serializers.ValidationError(
                "Write needs read, so read: false cannot be sent with write: true."
            )
        return attrs


class RoleFieldAccessPatchSerializer(serializers.Serializer):
    """The body of ``PATCH roles/<key>/field-access/``.

    The size limit is checked before any entry is validated, so an oversized
    body is refused without walking it.
    """

    changes = FieldAccessChangeSerializer(many=True, allow_empty=False)

    def to_internal_value(self, data):
        changes = data.get("changes") if isinstance(data, dict) else None
        if isinstance(changes, list) and len(changes) > MAX_FIELD_ACCESS_CHANGES:
            raise serializers.ValidationError({
                "changes": [
                    f"Send at most {MAX_FIELD_ACCESS_CHANGES} changes per request."
                ],
            })
        return super().to_internal_value(data)

    def validate_changes(self, value):
        seen, repeated = set(), set()
        for change in value:
            key = change["field"]
            (repeated if key in seen else seen).add(key)
        if repeated:
            raise serializers.ValidationError(
                f"Each field may appear once per request: {', '.join(sorted(repeated))}."
            )
        return value


class UserFieldAccessOverrideSerializer(serializers.ModelSerializer):
    """One field access exception on one user.

    ``field`` offers only the active fields the tenant in context may hold, so
    naming a platform field inside a school fails exactly as naming a field
    that does not exist, and reveals nothing about which it was.

    ``role_state`` is what the target's roles alone say about the field, read
    from a map the view builds once per request.
    """

    field = serializers.PrimaryKeyRelatedField(
        queryset=FieldDefinition.objects.none(),
        help_text="Dotted field key, e.g. 'procurement.vendor.bank_account_number'.",
    )
    field_key = serializers.CharField(source="field_id", read_only=True)
    field_label = serializers.CharField(source="field.label", read_only=True)

    user_id = serializers.SerializerMethodField()
    created_by_id = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    role_state = serializers.SerializerMethodField()

    class Meta:
        model = UserFieldAccessOverride
        fields = [
            "id",
            "user_id",
            "field",
            "field_key",
            "field_label",
            "access",
            "mode",
            "reason",
            "expires_at",
            "is_expired",
            "role_state",
            "created_by_id",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "user_id",
            "field_key",
            "field_label",
            "is_expired",
            "role_state",
            "created_by_id",
            "created_by_name",
            "created_at",
            "updated_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        queryset = FieldDefinition.objects.filter(is_active=True)
        if not tenant_is_platform(self.context.get("tenant")):
            queryset = queryset.filter(scope=PermissionScope.TENANT)
        fields["field"].queryset = queryset
        return fields

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

    def get_role_state(self, obj) -> dict | None:
        role_map = self.context.get("role_field_state")
        if role_map is None:
            return None
        return role_map.state(obj.field_id)

    def validate_reason(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A reason is required.")
        return value

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Expiry must be in the future.")
        return value

    def validate(self, attrs):
        """Refuse ``ALLOW WRITE`` on a field no API path can write.

        ``UserFieldAccessOverride.save()`` refuses it too; this is the layer
        that turns the refusal into a 400 with a field name on it.
        """
        attrs = super().validate(attrs)
        field = attrs.get("field")
        if (
            field is not None
            and not field.writable
            and attrs.get("mode") == UserFieldAccessOverride.Mode.ALLOW
            and attrs.get("access") == UserFieldAccessOverride.Access.WRITE
        ):
            raise serializers.ValidationError({
                "access": f"Field '{field.key}' is not writable, so write cannot be allowed.",
            })
        return attrs
