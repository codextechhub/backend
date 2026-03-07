from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from .models import (
    Permission,
    PermissionDependency,
    RoleTemplate,
    RolePermission,
    UserRoleAssignment,
    RoleVersionSnapshot,
    RoleChangeRequest,
    RoleChangeDeltaItem,
    RoleLockEvent,
    EffectivePermissionCache,
)


# -----------------------------------------------------------------------------
# 1) Permission Registry
# -----------------------------------------------------------------------------
class PermissionSerializer(serializers.ModelSerializer):
    """Read/write serializer for the global permission registry (Vision-owned)."""

    class Meta:
        model = Permission
        fields = [
            "key",
            "module_key",
            "action",
            "description",
            "sensitivity_level",
            "is_restricted",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class PermissionDependencySerializer(serializers.ModelSerializer):
    """
    Explains: Permission A depends on Permission B.

    We expose keys (strings) to keep it simple.
    """

    permission_key = serializers.CharField(source="permission.key")
    depends_on_key = serializers.CharField(source="depends_on.key")

    class Meta:
        model = PermissionDependency
        fields = [
            "id",
            "permission_key",
            "depends_on_key",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


# -----------------------------------------------------------------------------
# 2) Role Templates + Role Permissions
# -----------------------------------------------------------------------------
class RolePermissionSerializer(serializers.ModelSerializer):
    """
    One row inside a role: a permission and whether it's granted.

    We use permission_key instead of a nested object to keep it beginner-friendly.
    """

    permission_key = serializers.CharField(source="permission.key")

    class Meta:
        model = RolePermission
        fields = [
            "id",
            "permission_key",
            "granted",
            "granted_by",
            "granted_at",
        ]
        read_only_fields = ["id", "granted_by", "granted_at"]

    def validate_permission_key(self, value: str) -> str:
        # Basic cleanup
        v = (value or "").strip()
        if not v:
            raise serializers.ValidationError("permission_key is required.")
        return v


class RoleTemplateListSerializer(serializers.ModelSerializer):
    """
    Lightweight list serializer for roles overview screens.
    """

    assigned_users_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = RoleTemplate
        fields = [
            "id",
            "institution",
            "name",
            "status",
            "is_system_role",
            "is_locked",
            "version",
            "assigned_users_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["version", "created_at", "updated_at"]


class RoleTemplateDetailSerializer(serializers.ModelSerializer):
    """
    Role detail serializer.

    - Shows role permissions as a list of (permission_key, granted)
    - Supports simple create/update using a list of permission keys
    """

    role_permissions = RolePermissionSerializer(many=True, read_only=True)

    # For create/update, accept a list of permission keys (grants)
    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of permission keys to GRANT to this role.",
    )

    class Meta:
        model = RoleTemplate
        fields = [
            "id",
            "institution",
            "name",
            "description",
            "status",
            "is_system_role",
            "is_locked",
            "version",
            "created_by",
            "role_permissions",   # read-only expanded rows
            "permission_keys",    # write-only simple input
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["is_system_role", "version", "created_by", "created_at", "updated_at"]

    def validate_permission_keys(self, keys):
        """
        Beginner-friendly validation:
        - strip blanks
        - remove duplicates
        - ensure each permission exists in the registry
        """
        if keys is None:
            return []

        cleaned = []
        seen = set()
        for k in keys:
            k = (k or "").strip()
            if not k:
                continue
            if k in seen:
                continue
            cleaned.append(k)
            seen.add(k)

        if not cleaned:
            return []

        existing = set(Permission.objects.filter(key__in=cleaned).values_list("key", flat=True))
        missing = [k for k in cleaned if k not in existing]
        if missing:
            raise serializers.ValidationError(f"Unknown permission keys: {missing}")

        return cleaned

    @transaction.atomic
    def create(self, validated_data):
        """
        Create role + optional permission grants.
        We keep this easy: we write RolePermission rows for each permission key.
        """
        permission_keys = validated_data.pop("permission_keys", [])

        # created_by can be set from request user (common pattern)
        request = self.context.get("request")
        if request and request.user and request.user.is_authenticated:
            validated_data["created_by"] = request.user

        role = RoleTemplate.objects.create(**validated_data)

        # Add permissions
        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            RolePermission.objects.bulk_create(
                [RolePermission(role=role, permission=p, granted=True, granted_by=role.created_by) for p in perms]
            )

        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        """
        Update role fields + optionally replace permission grants (if permission_keys provided).
        """
        permission_keys = validated_data.pop("permission_keys", None)

        # Update normal fields
        for field, value in validated_data.items():
            setattr(instance, field, value)

        # If permissions are being updated, replace grants
        if permission_keys is not None:
            # bump version so caches can include it
            instance.bump_version()

        instance.save()

        if permission_keys is not None:
            # Clear existing grants, then re-add
            RolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)

            request = self.context.get("request")
            actor = request.user if request and request.user.is_authenticated else None

            RolePermission.objects.bulk_create(
                [RolePermission(role=instance, permission=p, granted=True, granted_by=actor) for p in perms]
            )

        return instance


# -----------------------------------------------------------------------------
# 3) Assign roles to users
# -----------------------------------------------------------------------------
class UserRoleAssignmentSerializer(serializers.ModelSerializer):
    """
    Assign or revoke a role for a user.

    Key beginner rule:
    - user, role, institution must be consistent (same institution context).
    """

    class Meta:
        model = UserRoleAssignment
        fields = [
            "id",
            "institution",
            "user",
            "role",
            "assignment_status",
            "assigned_by",
            "assigned_at",
            "revoked_at",
            "revoked_by",
            "reason_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "assigned_by",
            "assigned_at",
            "revoked_at",
            "revoked_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        institution = attrs.get("institution") or getattr(self.instance, "institution", None)
        role = attrs.get("role") or getattr(self.instance, "role", None)

        # Cross-institution safety: role must belong to institution
        if institution and role and role.institution_id != institution.id:
            raise serializers.ValidationError("Role must belong to the same institution as the assignment.")

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        # Store who assigned the role
        validated_data["assigned_by"] = actor

        return super().create(validated_data)


# -----------------------------------------------------------------------------
# 4) Role Version Snapshots (read-only in most APIs)
# -----------------------------------------------------------------------------
class RoleVersionSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleVersionSnapshot
        fields = [
            "id",
            "role",
            "version_number",
            "permissions_snapshot",
            "created_by",
            "reason",
            "created_at",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# 5) Role Change Requests (Institution -> Vision)
# -----------------------------------------------------------------------------
class RoleChangeDeltaItemSerializer(serializers.ModelSerializer):
    """
    One delta item: ADD or REMOVE a permission key.
    We accept permission as a key string for clarity.
    """

    permission_key = serializers.CharField(write_only=True)
    permission = PermissionSerializer(read_only=True)

    class Meta:
        model = RoleChangeDeltaItem
        fields = [
            "id",
            "permission_key",   # input
            "permission",       # output
            "operation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "permission", "created_at", "updated_at"]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key (not in registry).")
        return value


class RoleChangeRequestSerializer(serializers.ModelSerializer):
    """
    Create a request with delta items in one payload.

    Example input:
    {
      "institution": "...",
      "target_role": "...",
      "justification": "Need approve rights for term invoicing",
      "delta_items": [
        {"permission_key": "finance.invoice.approve", "operation": "ADD"},
        {"permission_key": "finance.invoice.export", "operation": "ADD"}
      ]
    }
    """

    delta_items = RoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = RoleChangeRequest
        fields = [
            "id",
            "institution",
            "requested_by",
            "target_role",
            "status",
            "justification",
            "reviewer",
            "reviewer_notes",
            "submitted_at",
            "decided_at",
            "impact_summary",
            "delta_items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "requested_by",
            "status",
            "reviewer",
            "reviewer_notes",
            "submitted_at",
            "decided_at",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        institution = attrs.get("institution")
        target_role = attrs.get("target_role")
        if institution and target_role and target_role.institution_id != institution.id:
            raise serializers.ValidationError("Target role must belong to the same institution as the request.")
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        delta_items_data = validated_data.pop("delta_items", [])

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        # requested_by always comes from auth user
        validated_data["requested_by"] = actor

        obj = RoleChangeRequest.objects.create(**validated_data)

        # Create delta items
        for item in delta_items_data:
            permission_key = item.pop("permission_key")
            perm = Permission.objects.get(key=permission_key)
            RoleChangeDeltaItem.objects.create(request=obj, permission=perm, **item)

        return obj


# -----------------------------------------------------------------------------
# 6) Critical role lock history (usually read-only)
# -----------------------------------------------------------------------------
class RoleLockEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleLockEvent
        fields = [
            "id",
            "role",
            "actor",
            "action",
            "reason",
            "created_at",
        ]
        read_only_fields = fields


# -----------------------------------------------------------------------------
# 7) Effective Permission Cache (optional; usually read-only)
# -----------------------------------------------------------------------------
class EffectivePermissionCacheSerializer(serializers.ModelSerializer):
    class Meta:
        model = EffectivePermissionCache
        fields = [
            "id",
            "institution",
            "user",
            "permissions_hash",
            "permissions",
            "computed_at",
            "expires_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields