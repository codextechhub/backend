"""Platform-level RBAC serializers: roles, assignments, change requests.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers
from django.utils import timezone

from ..models import (
    Permission,
    PermissionGroup,
    PlatformRoleChangeDeltaItem,
    PlatformRoleChangeRequest,
    PlatformRoleGroup,
    PlatformRolePermission,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
)


from .registry import (
    PermissionGroupListSerializer,
    PermissionKeyListValidationMixin,
    PermissionSerializer,
)

# -----------------------------------------------------------------------------
# 5) Platform Role Templates + Platform Role Permissions
# -----------------------------------------------------------------------------
class PlatformRolePermissionSerializer(serializers.ModelSerializer):
    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = PlatformRolePermission
        fields = [
            "id",
            "permission",
            "permission_key",
            "granted",
            "granted_by",
            "granted_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "permission_key",
            "granted_by",
            "granted_at",
            "created_at",
            "updated_at",
        ]


class PlatformRoleTemplateListSerializer(serializers.ModelSerializer):
    assigned_users_count = serializers.IntegerField(read_only=True)
    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PlatformRoleTemplate
        fields = [
            "id",
            "name",
            "status",
            "is_system_role",
            "is_locked",
            "version",
            "assigned_users_count",
            "permissions_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "version",
            "created_by",
            "created_at",
            "updated_at",
        ]


class PlatformRoleGroupAttachmentSerializer(serializers.ModelSerializer):
    """Read-only view of a permission group attached to a platform role."""

    group = PermissionGroupListSerializer(read_only=True)

    class Meta:
        model = PlatformRoleGroup
        fields = [
            "id",
            "group",
            "attached_by",
            "attached_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class PlatformRoleTemplateDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    """
    Detailed serializer for Vision/internal platform roles.

    Accepts both ``permission_keys`` (direct grants) and ``group_ids``
    (attached permission groups). Dependency validation runs against the
    flattened effective set.
    """

    role_permissions = PlatformRolePermissionSerializer(many=True, read_only=True)
    role_groups = PlatformRoleGroupAttachmentSerializer(many=True, read_only=True)

    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of permission keys to grant to this platform role template.",
    )
    group_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of permission group ids to attach to this platform role template.",
    )

    class Meta:
        model = PlatformRoleTemplate
        fields = [
            "id",
            "name",
            "description",
            "status",
            "is_system_role",
            "is_locked",
            "version",
            "created_by",
            "role_permissions",
            "role_groups",
            "permission_keys",
            "group_ids",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "is_system_role",
            "version",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def validate_name(self, value):
        qs = PlatformRoleTemplate.objects.filter(name__iexact=value)
        instance = self.instance
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(detail="An XVS staff role with this name already exists.")
        return value

    def validate_group_ids(self, value):
        if not value:
            return []

        seen = set()
        cleaned = []
        for gid in value:
            if gid in seen:
                continue
            cleaned.append(gid)
            seen.add(gid)

        existing = set(
            PermissionGroup.objects.filter(id__in=cleaned).values_list("id", flat=True)
        )
        missing = [str(gid) for gid in cleaned if gid not in existing]
        if missing:
            raise serializers.ValidationError(
                f"Unknown permission group ids: {missing}"
            )
        return cleaned

    @transaction.atomic
    def create(self, validated_data):
        permission_keys = validated_data.pop("permission_keys", [])
        group_ids = validated_data.pop("group_ids", [])

        # VALIDATE DEPENDENCIES against the flattened effective set
        if permission_keys or group_ids:
            from ..validators import validate_role_permissions
            validate_role_permissions(
                permission_keys=permission_keys,
                group_ids=group_ids,
            )

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        validated_data["created_by"] = actor
        role = PlatformRoleTemplate.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            PlatformRolePermission.objects.bulk_create(
                [
                    PlatformRolePermission(
                        role=role,
                        permission=perm,
                        granted=True,
                        granted_by=actor,
                    )
                    for perm in perms
                ]
            )

        if group_ids:
            groups = PermissionGroup.objects.filter(id__in=group_ids)
            PlatformRoleGroup.objects.bulk_create(
                [
                    PlatformRoleGroup(role=role, group=group, attached_by=actor)
                    for group in groups
                ]
            )

        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        permission_keys = validated_data.pop("permission_keys", None)
        group_ids = validated_data.pop("group_ids", None)

        if permission_keys is not None or group_ids is not None:
            effective_permission_keys = (
                permission_keys
                if permission_keys is not None
                else list(
                    PlatformRolePermission.objects.filter(
                        role=instance, granted=True
                    ).values_list("permission_id", flat=True)
                )
            )
            effective_group_ids = (
                group_ids
                if group_ids is not None
                else list(
                    PlatformRoleGroup.objects.filter(role=instance).values_list(
                        "group_id", flat=True
                    )
                )
            )

            if effective_permission_keys or effective_group_ids:
                from ..validators import validate_role_permissions
                validate_role_permissions(
                    permission_keys=effective_permission_keys,
                    group_ids=effective_group_ids,
                )

        for field, value in validated_data.items():
            setattr(instance, field, value)

        if permission_keys is not None or group_ids is not None:
            instance.bump_version()

        instance.save()

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if permission_keys is not None:
            PlatformRolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            PlatformRolePermission.objects.bulk_create(
                [
                    PlatformRolePermission(
                        role=instance,
                        permission=perm,
                        granted=True,
                        granted_by=actor,
                    )
                    for perm in perms
                ]
            )

        if group_ids is not None:
            PlatformRoleGroup.objects.filter(role=instance).delete()
            groups = PermissionGroup.objects.filter(id__in=group_ids)
            PlatformRoleGroup.objects.bulk_create(
                [
                    PlatformRoleGroup(role=instance, group=group, attached_by=actor)
                    for group in groups
                ]
            )

        return instance


# -----------------------------------------------------------------------------
# 6) Platform User Role Assignments
# -----------------------------------------------------------------------------
class PlatformUserRoleAssignmentSerializer(serializers.ModelSerializer):
    """
    Assign or revoke a platform role for a Vision/internal user.
    """

    # Write-only FK fields used on create/update.
    user = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(),
        write_only=True,
    )
    role = serializers.PrimaryKeyRelatedField(
        queryset=PlatformRoleTemplate.objects.all(),
        write_only=True,
    )

    # Read-only expanded fields derived from select_related data.
    user_id = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    role_id = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    assigned_by_id = serializers.SerializerMethodField()
    assigned_by_name = serializers.SerializerMethodField()
    revoked_by_id = serializers.SerializerMethodField()
    revoked_by_name = serializers.SerializerMethodField()

    def get_user_id(self, obj):
        return str(obj.user_id) if obj.user_id else None

    def get_user_name(self, obj):
        return getattr(obj.user, "full_name", None) or getattr(obj.user, "email", None)

    def get_user_email(self, obj):
        return getattr(obj.user, "email", None)

    def get_role_id(self, obj):
        return str(obj.role_id) if obj.role_id else None

    def get_role_name(self, obj):
        return getattr(obj.role, "name", None)

    def get_assigned_by_id(self, obj):
        return str(obj.assigned_by_id) if obj.assigned_by_id else None

    def get_assigned_by_name(self, obj):
        return getattr(obj.assigned_by, "full_name", None) or getattr(obj.assigned_by, "email", None)

    def get_revoked_by_id(self, obj):
        return str(obj.revoked_by_id) if obj.revoked_by_id else None

    def get_revoked_by_name(self, obj):
        return getattr(obj.revoked_by, "full_name", None) or getattr(obj.revoked_by, "email", None)

    class Meta:
        model = PlatformUserRoleAssignment
        fields = [
            "id",
            "user",
            "role",
            "user_id",
            "user_name",
            "user_email",
            "role_id",
            "role_name",
            "assignment_status",
            "assigned_by_id",
            "assigned_by_name",
            "assigned_at",
            "revoked_at",
            "revoked_by_id",
            "revoked_by_name",
            "reason_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "user_id",
            "user_name",
            "user_email",
            "role_id",
            "role_name",
            "assigned_by_id",
            "assigned_by_name",
            "assigned_at",
            "revoked_at",
            "revoked_by_id",
            "revoked_by_name",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        user = attrs.get("user", getattr(self.instance, "user", None))
        role = attrs.get("role", getattr(self.instance, "role", None))
        new_status = attrs.get(
            "assignment_status",
            getattr(
                self.instance,
                "assignment_status",
                PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
            ),
        )

        if not actor:
            raise serializers.ValidationError({"assigned_by": "Authenticated actor is required."})

        if not user:
            raise serializers.ValidationError({"user": "User is required."})

        if not role:
            raise serializers.ValidationError({"role": "Role is required."})

        # 1. Platform roles should only be assigned to Vision/internal users.
        if not getattr(user, "is_vision_staff", False) and not getattr(user, "is_staff", False):
            raise serializers.ValidationError(
                {"user": "Platform roles can only be assigned to Vision/internal users."}
            )

        # 2. Prevent users from assigning platform roles to themselves.
        if self.instance is None and user == actor:
            raise serializers.ValidationError(
                {"user": "You cannot assign a platform role to yourself."}
            )

        # 3. Prevent duplicate active assignment.
        qs = PlatformUserRoleAssignment.objects.filter(
            user=user,
            role=role,
            assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
        )

        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)

        if new_status == PlatformUserRoleAssignment.AssignmentStatus.ACTIVE and qs.exists():
            raise serializers.ValidationError(
                {
                    "role": (
                        "This user already has an active assignment "
                        "for this platform role."
                    )
                }
            )

        # 4. Prevent revoked assignment from being reactivated through normal update.
        if (
            self.instance
            and self.instance.assignment_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED
            and new_status == PlatformUserRoleAssignment.AssignmentStatus.ACTIVE
        ):
            raise serializers.ValidationError(
                {"role": "A revoked platform role assignment cannot be reactivated. Create a new assignment instead."}
            )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        validated_data["assigned_by"] = actor

        # Update role for existing active assignment if exists, otherwise create new assignment.
        if validated_data["user"] and validated_data["role"]:      
            existing_qs = PlatformUserRoleAssignment.objects.filter(
                user=validated_data["user"],
                assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
            )
            if existing_qs.exists():
                existing_qs.update(role=validated_data["role"], assigned_by=actor, assigned_at=timezone.now(), updated_at=timezone.now())
                return existing_qs.first()

        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        new_status = validated_data.get("assignment_status", instance.assignment_status)

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if (
            new_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED
            and instance.assignment_status
            != PlatformUserRoleAssignment.AssignmentStatus.REVOKED
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
        fields = [
            "id",
            "permission_key",
            "permission",
            "operation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "permission",
            "created_at",
            "updated_at",
        ]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class PlatformRoleChangeRequestSerializer(serializers.ModelSerializer):
    """
    Create a platform role change request with delta items.
    """

    delta_items = PlatformRoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = PlatformRoleChangeRequest
        fields = [
            "id",
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
        if not attrs.get("delta_items"):
            raise serializers.ValidationError(
                {"delta_items": "At least one delta item is required."}
            )
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
            PlatformRoleChangeDeltaItem.objects.create(
                request=obj,
                permission=perm,
                **item,
            )

        return obj
