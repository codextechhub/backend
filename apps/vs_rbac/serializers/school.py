"""School-level RBAC serializers: roles, assignments, change requests.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from ..models import (
    Permission,
    PermissionGroup,
    SchoolRoleChangeDeltaItem,
    SchoolRoleChangeRequest,
    SchoolRoleGroup,
    SchoolRolePermission,
    SchoolRoleTemplate,
    SchoolUserRoleAssignment,
)


from .registry import (
    PermissionGroupListSerializer,
    PermissionKeyListValidationMixin,
    PermissionSerializer,
)

# -----------------------------------------------------------------------------
# 2) School Role Templates + Role Permissions
# -----------------------------------------------------------------------------
class SchoolRolePermissionSerializer(serializers.ModelSerializer):
    """
    One permission row attached to a school role template.
    """

    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = SchoolRolePermission
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


class SchoolRolePermissionWriteSerializer(serializers.ModelSerializer):
    """
    Beginner-friendly write serializer for a single role permission row.
    """

    permission_key = serializers.CharField(write_only=True)

    class Meta:
        model = SchoolRolePermission
        fields = [
            "id",
            "permission_key",
            "granted",
            "granted_by",
            "granted_at",
        ]
        read_only_fields = ["id", "granted_by", "granted_at"]

    def validate_permission_key(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("permission_key is required.")
        if not Permission.objects.filter(key=value).exists():
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class SchoolRoleTemplateListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for list screens.
    """

    assigned_users_count = serializers.IntegerField(read_only=True)
    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SchoolRoleTemplate
        fields = [
            "id",
            "school",
            "branch",
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


class SchoolRoleGroupAttachmentSerializer(serializers.ModelSerializer):
    """Read-only view of a permission group attached to a school role."""

    group = PermissionGroupListSerializer(read_only=True)

    class Meta:
        model = SchoolRoleGroup
        fields = [
            "id",
            "group",
            "attached_by",
            "attached_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class SchoolRoleTemplateDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    """
    Detailed serializer for school role templates.

    Read:
    - shows expanded role_permissions
    - shows attached permission groups via ``role_groups``

    Write:
    - accepts permission_keys = ["finance.invoice.view", "finance.invoice.approve"]
      which replaces the role's direct permission rows
    - accepts group_ids = ["<uuid>", "<uuid>"] which replaces the role's
      attached permission groups
    - dependency validation runs against the *flattened* effective set
      (direct permissions + group-derived permissions) so permissions required
      by dependencies can be satisfied via either source.
    """

    role_permissions = SchoolRolePermissionSerializer(many=True, read_only=True)
    role_groups = SchoolRoleGroupAttachmentSerializer(many=True, read_only=True)

    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of permission keys to grant to this school role template.",
    )
    group_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of permission group ids to attach to this school role template.",
    )

    class Meta:
        model = SchoolRoleTemplate
        fields = [
            "id",
            "school",
            "branch",
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

    def validate(self, attrs):
        school = attrs.get("school") or getattr(self.instance, "school", None)
        if not school:
            raise serializers.ValidationError({"school": "school is required."})
        name = attrs.get("name") or getattr(self.instance, "name", None)
        if name and school:
            qs = SchoolRoleTemplate.objects.filter(school=school, name__iexact=name)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"name": "A role with this name already exists in this school."})
        return attrs

    def validate_group_ids(self, value):
        if not value:
            return []

        # dedupe while preserving order
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
        role = SchoolRoleTemplate.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            SchoolRolePermission.objects.bulk_create(
                [
                    SchoolRolePermission(
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
            SchoolRoleGroup.objects.bulk_create(
                [
                    SchoolRoleGroup(role=role, group=group, attached_by=actor)
                    for group in groups
                ]
            )

        return role

    @transaction.atomic
    def update(self, instance, validated_data):
        permission_keys = validated_data.pop("permission_keys", None)
        group_ids = validated_data.pop("group_ids", None)

        # Build the effective set we'd end up with so dependency validation
        # sees both the direct grants *and* the group-derived grants.
        if permission_keys is not None or group_ids is not None:
            effective_permission_keys = (
                permission_keys
                if permission_keys is not None
                else list(
                    SchoolRolePermission.objects.filter(
                        role=instance, granted=True
                    ).values_list("permission_id", flat=True)
                )
            )
            effective_group_ids = (
                group_ids
                if group_ids is not None
                else list(
                    SchoolRoleGroup.objects.filter(role=instance).values_list(
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
            SchoolRolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            SchoolRolePermission.objects.bulk_create(
                [
                    SchoolRolePermission(
                        role=instance,
                        permission=perm,
                        granted=True,
                        granted_by=actor,
                    )
                    for perm in perms
                ]
            )

        if group_ids is not None:
            SchoolRoleGroup.objects.filter(role=instance).delete()
            groups = PermissionGroup.objects.filter(id__in=group_ids)
            SchoolRoleGroup.objects.bulk_create(
                [
                    SchoolRoleGroup(role=instance, group=group, attached_by=actor)
                    for group in groups
                ]
            )

        return instance


# -----------------------------------------------------------------------------
# 3) School User Role Assignments
# -----------------------------------------------------------------------------
class SchoolUserRoleAssignmentSerializer(serializers.ModelSerializer):
    """
    Assign or revoke a school role for a user.

    Key rule:
    - role.school must match assignment.school
    """

    class Meta:
        model = SchoolUserRoleAssignment
        fields = [
            "id",
            "school",
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
        school = attrs.get("school") or getattr(self.instance, "school", None)
        role = attrs.get("role") or getattr(self.instance, "role", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        new_status = attrs.get(
            "assignment_status",
            getattr(self.instance, "assignment_status", SchoolUserRoleAssignment.AssignmentStatus.ACTIVE),
        )

        if school and role and role.school_id != school.pk:
            raise serializers.ValidationError(
                {"role": "Role must belong to the same school as the assignment."}
            )

        if new_status == SchoolUserRoleAssignment.AssignmentStatus.ACTIVE and school and user and role:
            qs = SchoolUserRoleAssignment.objects.filter(
                school=school,
                user=user,
                role=role,
                assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"role": "This user already has an active assignment for this role in this school."}
                )

        if (
            self.instance
            and self.instance.assignment_status == SchoolUserRoleAssignment.AssignmentStatus.REVOKED
            and new_status == SchoolUserRoleAssignment.AssignmentStatus.ACTIVE
        ):
            raise serializers.ValidationError(
                {"assignment_status": "A revoked assignment cannot be reactivated. Create a new assignment instead."}
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

        # If changing to REVOKED and it wasn't revoked before, stamp revoke info
        if (
            new_status == SchoolUserRoleAssignment.AssignmentStatus.REVOKED
            and instance.assignment_status != SchoolUserRoleAssignment.AssignmentStatus.REVOKED
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
# 4) School Role Change Requests
# -----------------------------------------------------------------------------
class SchoolRoleChangeDeltaItemSerializer(serializers.ModelSerializer):
    """
    One requested change item:
    - ADD permission
    - REMOVE permission
    """

    permission_key = serializers.CharField(write_only=True)
    permission = PermissionSerializer(read_only=True)

    class Meta:
        model = SchoolRoleChangeDeltaItem
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


class SchoolRoleChangeRequestSerializer(serializers.ModelSerializer):
    """
    Create a school-level role change request with delta items.

    Example input:
    {
      "school": 1,
      "target_role": 5,
      "justification": "Need invoice approval permissions",
      "delta_items": [
        {"permission_key": "finance.invoice.approve", "operation": "ADD"},
        {"permission_key": "finance.invoice.export", "operation": "ADD"}
      ]
    }
    """

    delta_items = SchoolRoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = SchoolRoleChangeRequest
        fields = [
            "id",
            "school",
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
        school = attrs.get("school") or getattr(self.instance, "school", None)
        target_role = attrs.get("target_role") or getattr(self.instance, "target_role", None)

        if school and target_role and target_role.school_id != school.pk:
            raise serializers.ValidationError(
                {"target_role": "Target role must belong to the same school as the request."}
            )

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
        obj = SchoolRoleChangeRequest.objects.create(**validated_data)

        for item in delta_items_data:
            permission_key = item.pop("permission_key")
            perm = Permission.objects.get(key=permission_key)
            SchoolRoleChangeDeltaItem.objects.create(
                request=obj,
                permission=perm,
                **item,
            )

        return obj


