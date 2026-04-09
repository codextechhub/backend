from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from .models import (
    Permission,
    PermissionDependency,
    RoleTemplate,
    RolePermission,
    UserRoleAssignment,
    RoleChangeRequest,
    RoleChangeDeltaItem,
    PlatformRoleTemplate,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
    PlatformRoleChangeRequest,
    PlatformRoleChangeDeltaItem,
)


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
class PermissionKeyListValidationMixin:
    """
    Reusable helper for serializers that accept a list of permission keys.
    Strips whitespace, removes blanks/duplicates, confirms keys exist.
    """

    def validate_permission_keys(self, keys):
        if keys is None:
            return []

        cleaned = []
        seen = set()

        for key in keys:
            key = (key or "").strip()
            if not key or key in seen:
                continue
            cleaned.append(key)
            seen.add(key)

        if not cleaned:
            return []

        existing = set(
            Permission.objects.filter(key__in=cleaned).values_list("key", flat=True)
        )
        missing = [key for key in cleaned if key not in existing]
        if missing:
            raise serializers.ValidationError(f"Unknown permission keys: {missing}")

        return cleaned


def _check_restricted_permissions(permission_keys):
    """Raise if any permission keys are restricted (require approval workflow)."""
    if not permission_keys:
        return
    restricted = list(
        Permission.objects.filter(
            key__in=permission_keys, is_restricted=True
        ).values_list("key", flat=True)
    )
    if restricted:
        raise serializers.ValidationError({
            "permission_keys": [
                f"Restricted permissions cannot be granted directly "
                f"(use a RoleChangeRequest): {restricted}"
            ]
        })


def _check_inactive_permissions(permission_keys):
    """Raise if any permission keys are inactive."""
    if not permission_keys:
        return
    inactive = list(
        Permission.objects.filter(
            key__in=permission_keys, is_active=False
        ).values_list("key", flat=True)
    )
    if inactive:
        raise serializers.ValidationError({
            "permission_keys": [f"Inactive permissions cannot be granted: {inactive}"]
        })


def _check_role_editable(instance):
    """Raise if role is locked or a system role."""
    if instance is None:
        return
    if instance.is_locked:
        raise serializers.ValidationError("This role is locked and cannot be modified.")
    if instance.is_system_role:
        raise serializers.ValidationError("System roles cannot be modified by institution admins.")


# -----------------------------------------------------------------------------
# 1) Permission Registry
# -----------------------------------------------------------------------------
class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = [
            "key", "module_key", "action", "description",
            "sensitivity_level", "is_restricted", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class PermissionDependencySerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(source="permission.key")
    depends_on_key = serializers.CharField(source="depends_on.key")

    class Meta:
        model = PermissionDependency
        fields = ["id", "permission_key", "depends_on_key", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


# -----------------------------------------------------------------------------
# 2) Institution Role Templates + Role Permissions
# -----------------------------------------------------------------------------
class RolePermissionSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = RolePermission
        fields = [
            "id", "permission", "permission_key", "granted",
            "granted_by", "granted_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "permission_key", "granted_by", "granted_at",
            "created_at", "updated_at",
        ]


class RolePermissionWriteSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(write_only=True)

    class Meta:
        model = RolePermission
        fields = ["id", "permission_key", "granted", "granted_by", "granted_at"]
        read_only_fields = ["id", "granted_by", "granted_at"]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class RoleTemplateListSerializer(serializers.ModelSerializer):
    assigned_users_count = serializers.IntegerField(read_only=True)
    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = RoleTemplate
        fields = [
            "id", "institution", "name", "status", "is_system_role", "is_locked",
            "version", "assigned_users_count", "permissions_count",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["version", "created_by", "created_at", "updated_at"]


class RoleTemplateDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    role_permissions = RolePermissionSerializer(many=True, read_only=True)
    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
    )

    class Meta:
        model = RoleTemplate
        fields = [
            "id", "institution", "name", "description", "status",
            "is_system_role", "is_locked", "version", "created_by",
            "role_permissions", "permission_keys", "created_at", "updated_at",
        ]
        read_only_fields = [
            "is_system_role", "version", "created_by", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        institution = attrs.get("institution") or getattr(self.instance, "institution", None)
        if not institution:
            raise serializers.ValidationError({"institution": "institution is required."})

        # Block edits on locked/system roles
        if self.instance:
            _check_role_editable(self.instance)

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        permission_keys = validated_data.pop("permission_keys", [])

        if permission_keys:
            _check_inactive_permissions(permission_keys)
            _check_restricted_permissions(permission_keys)
            from .validators import validate_role_permissions
            validate_role_permissions(permission_keys)

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["created_by"] = actor
        role = RoleTemplate.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            RolePermission.objects.bulk_create([
                RolePermission(role=role, permission=perm, granted=True, granted_by=actor)
                for perm in perms
            ])

        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        permission_keys = validated_data.pop("permission_keys", None)

        if permission_keys:
            _check_inactive_permissions(permission_keys)
            _check_restricted_permissions(permission_keys)
            from .validators import validate_role_permissions
            validate_role_permissions(permission_keys)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        if permission_keys is not None:
            instance.bump_version()

        instance.save()

        if permission_keys is not None:
            request = self.context.get("request")
            actor = request.user if request and request.user.is_authenticated else None
            RolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            RolePermission.objects.bulk_create([
                RolePermission(role=instance, permission=perm, granted=True, granted_by=actor)
                for perm in perms
            ])

        return instance


# -----------------------------------------------------------------------------
# 3) Institution User Role Assignments
# -----------------------------------------------------------------------------
class UserRoleAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserRoleAssignment
        fields = [
            "id", "institution", "user", "role", "assignment_status",
            "assigned_by", "assigned_at", "revoked_at", "revoked_by",
            "reason_note", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "assigned_by", "assigned_at", "revoked_at", "revoked_by",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        institution = attrs.get("institution") or getattr(self.instance, "institution", None)
        role = attrs.get("role") or getattr(self.instance, "role", None)

        if institution and role and role.institution_id != institution.pk:
            raise serializers.ValidationError(
                "Role must belong to the same institution as the assignment."
            )

        # Block assignment to non-ACTIVE roles
        if role and role.status != RoleTemplate.Status.ACTIVE:
            raise serializers.ValidationError(
                f"Cannot assign a role with status '{role.status}'. Role must be ACTIVE."
            )

        # Block REVOKED -> ACTIVE reactivation
        if self.instance:
            new_status = attrs.get("assignment_status", self.instance.assignment_status)
            if (
                self.instance.assignment_status == UserRoleAssignment.AssignmentStatus.REVOKED
                and new_status == UserRoleAssignment.AssignmentStatus.ACTIVE
            ):
                raise serializers.ValidationError(
                    "Cannot reactivate a revoked assignment. Create a new assignment instead."
                )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["assigned_by"] = actor
        return super().create(validated_data)

    def update(self, instance, validated_data):
        new_status = validated_data.get("assignment_status", instance.assignment_status)
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if (
            new_status == UserRoleAssignment.AssignmentStatus.REVOKED
            and instance.assignment_status != UserRoleAssignment.AssignmentStatus.REVOKED
        ):
            instance.revoke(
                by_user=actor,
                reason=validated_data.get("reason_note", instance.reason_note),
            )

        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.save()
        return instance


# -----------------------------------------------------------------------------
# 4) Institution Role Change Requests
# -----------------------------------------------------------------------------
class RoleChangeDeltaItemSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(write_only=True)
    permission = PermissionSerializer(read_only=True)

    class Meta:
        model = RoleChangeDeltaItem
        fields = ["id", "permission_key", "permission", "operation", "created_at", "updated_at"]
        read_only_fields = ["id", "permission", "created_at", "updated_at"]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class RoleChangeRequestSerializer(serializers.ModelSerializer):
    delta_items = RoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = RoleChangeRequest
        fields = [
            "id", "institution", "requested_by", "target_role", "status",
            "justification", "reviewer", "reviewer_notes", "submitted_at",
            "decided_at", "impact_summary", "delta_items", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "requested_by", "status", "reviewer", "reviewer_notes",
            "submitted_at", "decided_at", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        institution = attrs.get("institution") or getattr(self.instance, "institution", None)
        target_role = attrs.get("target_role") or getattr(self.instance, "target_role", None)

        if institution and target_role and target_role.institution_id != institution.pk:
            raise serializers.ValidationError(
                "Target role must belong to the same institution as the request."
            )

        justification = attrs.get("justification", "")
        if not justification or not justification.strip():
            raise serializers.ValidationError({"justification": "Justification is required."})

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        delta_items_data = validated_data.pop("delta_items", [])
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["requested_by"] = actor
        obj = RoleChangeRequest.objects.create(**validated_data)

        for item in delta_items_data:
            permission_key = item.pop("permission_key")
            perm = Permission.objects.get(key=permission_key)
            RoleChangeDeltaItem.objects.create(request=obj, permission=perm, **item)

        return obj


# -----------------------------------------------------------------------------
# 5) Platform Role Templates + Platform Role Permissions
# -----------------------------------------------------------------------------
class PlatformRolePermissionSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = PlatformRolePermission
        fields = [
            "id", "permission", "permission_key", "granted",
            "granted_by", "granted_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "permission_key", "granted_by", "granted_at",
            "created_at", "updated_at",
        ]


class PlatformRoleTemplateListSerializer(serializers.ModelSerializer):
    assigned_users_count = serializers.IntegerField(read_only=True)
    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PlatformRoleTemplate
        fields = [
            "id", "name", "status", "is_system_role", "is_locked", "version",
            "assigned_users_count", "permissions_count",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["version", "created_by", "created_at", "updated_at"]


class PlatformRoleTemplateDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    role_permissions = PlatformRolePermissionSerializer(many=True, read_only=True)
    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
    )

    class Meta:
        model = PlatformRoleTemplate
        fields = [
            "id", "name", "description", "status", "is_system_role", "is_locked",
            "version", "created_by", "role_permissions", "permission_keys",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "is_system_role", "version", "created_by", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        if self.instance and self.instance.is_locked:
            raise serializers.ValidationError("This platform role is locked and cannot be modified.")
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        permission_keys = validated_data.pop("permission_keys", [])

        if permission_keys:
            _check_inactive_permissions(permission_keys)
            from .validators import validate_role_permissions
            validate_role_permissions(permission_keys)

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["created_by"] = actor
        role = PlatformRoleTemplate.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            PlatformRolePermission.objects.bulk_create([
                PlatformRolePermission(role=role, permission=perm, granted=True, granted_by=actor)
                for perm in perms
            ])

        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        permission_keys = validated_data.pop("permission_keys", None)

        if permission_keys:
            _check_inactive_permissions(permission_keys)
            from .validators import validate_role_permissions
            validate_role_permissions(permission_keys)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        if permission_keys is not None:
            instance.bump_version()

        instance.save()

        if permission_keys is not None:
            request = self.context.get("request")
            actor = request.user if request and request.user.is_authenticated else None
            PlatformRolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            PlatformRolePermission.objects.bulk_create([
                PlatformRolePermission(role=instance, permission=perm, granted=True, granted_by=actor)
                for perm in perms
            ])

        return instance


# -----------------------------------------------------------------------------
# 6) Platform User Role Assignments
# -----------------------------------------------------------------------------
class PlatformUserRoleAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformUserRoleAssignment
        fields = [
            "id", "user", "role", "assignment_status", "assigned_by",
            "assigned_at", "revoked_at", "revoked_by", "reason_note",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "assigned_by", "assigned_at", "revoked_at", "revoked_by",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        role = attrs.get("role") or getattr(self.instance, "role", None)

        if role and role.status != PlatformRoleTemplate.Status.ACTIVE:
            raise serializers.ValidationError(
                f"Cannot assign a role with status '{role.status}'. Role must be ACTIVE."
            )

        if self.instance:
            new_status = attrs.get("assignment_status", self.instance.assignment_status)
            if (
                self.instance.assignment_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED
                and new_status == PlatformUserRoleAssignment.AssignmentStatus.ACTIVE
            ):
                raise serializers.ValidationError(
                    "Cannot reactivate a revoked assignment. Create a new assignment instead."
                )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["assigned_by"] = actor
        return super().create(validated_data)

    def update(self, instance, validated_data):
        new_status = validated_data.get("assignment_status", instance.assignment_status)
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if (
            new_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED
            and instance.assignment_status != PlatformUserRoleAssignment.AssignmentStatus.REVOKED
        ):
            instance.revoke(
                by_user=actor,
                reason=validated_data.get("reason_note", instance.reason_note),
            )

        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.save()
        return instance


# -----------------------------------------------------------------------------
# 7) Platform Role Change Requests
# -----------------------------------------------------------------------------
class PlatformRoleChangeDeltaItemSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(write_only=True)
    permission = PermissionSerializer(read_only=True)

    class Meta:
        model = PlatformRoleChangeDeltaItem
        fields = ["id", "permission_key", "permission", "operation", "created_at", "updated_at"]
        read_only_fields = ["id", "permission", "created_at", "updated_at"]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class PlatformRoleChangeRequestSerializer(serializers.ModelSerializer):
    delta_items = PlatformRoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = PlatformRoleChangeRequest
        fields = [
            "id", "requested_by", "target_role", "status", "justification",
            "reviewer", "reviewer_notes", "submitted_at", "decided_at",
            "impact_summary", "delta_items", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "requested_by", "status", "reviewer", "reviewer_notes",
            "submitted_at", "decided_at", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        justification = attrs.get("justification", "")
        if not justification or not justification.strip():
            raise serializers.ValidationError({"justification": "Justification is required."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        delta_items_data = validated_data.pop("delta_items", [])
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["requested_by"] = actor
        obj = PlatformRoleChangeRequest.objects.create(**validated_data)

        for item in delta_items_data:
            permission_key = item.pop("permission_key")
            perm = Permission.objects.get(key=permission_key)
            PlatformRoleChangeDeltaItem.objects.create(request=obj, permission=perm, **item)

        return obj
