from __future__ import annotations

from django.db import transaction
from rest_framework import serializers
from django.utils import timezone

from .models import (
    GroupPermission,
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    PlatformRoleChangeDeltaItem,
    PlatformRoleChangeRequest,
    PlatformRoleGroup,
    PlatformRolePermission,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    SchoolRoleChangeDeltaItem,
    SchoolRoleChangeRequest,
    SchoolRoleGroup,
    SchoolRolePermission,
    SchoolRoleTemplate,
    SchoolUserRoleAssignment,
)


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
class PermissionKeyListValidationMixin:
    """
    Reusable helper for serializers that accept a list of permission keys.

    What it does:
    - strips whitespace
    - removes blanks
    - removes duplicates while preserving order
    - confirms all permission keys exist
    """

    def validate_permission_keys(self, keys):
        if keys is None:
            return []

        cleaned = []
        seen = set()

        for key in keys:
            key = (key or "").strip()
            if not key:
                continue
            if key in seen:
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
            raise serializers.ValidationError(
                f"Unknown permission keys: {missing}"
            )

        return cleaned


# -----------------------------------------------------------------------------
# 1) Permission vocabulary — Module / Resource / Action
# -----------------------------------------------------------------------------

class PermissionModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PermissionModule
        fields = ["name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class PermissionResourceSerializer(serializers.ModelSerializer):
    module = serializers.SlugRelatedField(
        slug_field="name",
        queryset=PermissionModule.objects.filter(is_active=True),
    )

    class Meta:
        model = PermissionResource
        fields = ["id", "module", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        module = attrs.get("module") or getattr(self.instance, "module", None)
        name = attrs.get("name") or getattr(self.instance, "name", None)
        qs = PermissionResource.objects.filter(module=module, name=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "A resource with this name already exists in this module."})
        return attrs


class PermissionActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PermissionAction
        fields = ["name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


# -----------------------------------------------------------------------------
# 1b) Permission Registry
# -----------------------------------------------------------------------------
class PermissionSerializer(serializers.ModelSerializer):
    """Read/write serializer for the global permission registry."""

    module = serializers.SlugRelatedField(
        slug_field="name",
        queryset=PermissionModule.objects.filter(is_active=True),
    )
    # Accepts the resource name slug on write (e.g. "invoice").
    # Resolved to a PermissionResource instance in validate() using the module context.
    # write_only because the read representation uses resource_key instead.
    resource = serializers.CharField(
        write_only=True,
        help_text="Resource name slug. Must belong to the selected module.",
    )
    action = serializers.SlugRelatedField(
        slug_field="name",
        queryset=PermissionAction.objects.filter(is_active=True),
    )

    resource_key = serializers.SerializerMethodField(read_only=True)
    module_key = serializers.SerializerMethodField(read_only=True)
    action_key = serializers.SerializerMethodField(read_only=True)

    def get_resource_key(self, obj):
        return obj.resource.name if obj.resource_id else None

    def get_module_key(self, obj):
        return obj.module_id

    def get_action_key(self, obj):
        return obj.action_id

    class Meta:
        model = Permission
        fields = [
            "key",
            "module",
            "module_key",
            "resource",
            "resource_key",
            "action",
            "action_key",
            "description",
            "sensitivity_level",
            "is_restricted",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["key", "module_key", "resource_key", "action_key", "created_at", "updated_at"]

    def validate(self, attrs):
        module = attrs.get("module") or getattr(self.instance, "module", None)
        resource_name = attrs.get("resource")

        # Resolve resource name → PermissionResource instance using module context.
        # resource.name is only unique per-module, so both values are required.
        if resource_name:
            if not module:
                raise serializers.ValidationError(
                    {"module": "Module is required to resolve the resource."}
                )
            try:
                resource_obj = PermissionResource.objects.get(
                    module_id=module.pk, name=resource_name, is_active=True
                )
                attrs["resource"] = resource_obj
            except PermissionResource.DoesNotExist:
                raise serializers.ValidationError({
                    "resource": (
                        f"No active resource '{resource_name}' found in module '{module.name}'. "
                        "Create the resource first or choose a different module."
                    )
                })

        resource = attrs.get("resource") or getattr(self.instance, "resource", None)
        action = attrs.get("action") or getattr(self.instance, "action", None)

        # Module–resource ownership check (guards against direct API misuse)
        if (
            module
            and isinstance(resource, PermissionResource)
            and resource.module_id != module.pk
        ):
            raise serializers.ValidationError({
                "resource": f"Resource '{resource.name}' does not belong to module '{module.name}'."
            })

        # Duplicate key guard — checks the composed key before hitting the DB unique constraint
        if module and isinstance(resource, PermissionResource) and action:
            composed_key = f"{module.pk}.{resource.name}.{action.pk}"
            qs = Permission.objects.filter(key=composed_key)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({
                    "key": f'Permission "{composed_key}" already exists.'
                })

        return attrs


class PermissionDependencySerializer(serializers.ModelSerializer):
    """
    Permission A depends on Permission B.

    We expose simple keys instead of nested objects.
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

    def create(self, validated_data):
        permission_key = validated_data.pop("permission", {}).get("key")
        depends_on_key = validated_data.pop("depends_on", {}).get("key")
        permission = Permission.objects.get(key=permission_key)
        depends_on = Permission.objects.get(key=depends_on_key)
        return PermissionDependency.objects.create(
            permission=permission,
            depends_on=depends_on,
            **validated_data,
        )


class PermissionDetailSerializer(PermissionSerializer):
    """Extended serializer for the permission detail endpoint.

    Adds groups this permission belongs to, permissions it depends on
    (dependencies), and permissions that depend on it (dependents).
    """

    groups = serializers.SerializerMethodField()
    dependencies = serializers.SerializerMethodField()
    dependents = serializers.SerializerMethodField()

    class Meta(PermissionSerializer.Meta):
        fields = PermissionSerializer.Meta.fields + ["groups", "dependencies", "dependents"]

    def get_groups(self, obj):
        return [
            {"id": str(g.id), "name": g.name, "is_system": g.is_system}
            for g in obj.groups.all()
        ]

    def get_dependencies(self, obj):
        return [
            {"key": dep.depends_on.key, "description": dep.depends_on.description}
            for dep in obj.dependencies.select_related("depends_on").all()
        ]

    def get_dependents(self, obj):
        return [
            {"key": dep.permission.key, "description": dep.permission.description}
            for dep in obj.required_by.select_related("permission").all()
        ]


# -----------------------------------------------------------------------------
# 1b) Permission Groups — reusable permission bundles shared across school and
#     platform role templates.
# -----------------------------------------------------------------------------
class PermissionGroupListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for permission group list screens."""

    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PermissionGroup
        fields = [
            "id",
            "name",
            "description",
            "is_system",
            "is_active",
            "permissions_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_system",
            "permissions_count",
            "created_at",
            "updated_at",
        ]


class PermissionGroupDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    """
    Detailed serializer for a permission group.

    Read:
    - ``permissions`` expands to the full ``Permission`` rows in the group.

    Write:
    - ``permission_keys`` replaces the group's membership with the given set.
    """

    permissions = PermissionSerializer(many=True, read_only=True)

    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of permission keys that should belong to this group.",
    )

    class Meta:
        model = PermissionGroup
        fields = [
            "id",
            "name",
            "description",
            "is_system",
            "is_active",
            "permissions",
            "permission_keys",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_system",
            "created_at",
            "updated_at",
        ]

    def validate_name(self, value):
        qs = PermissionGroup.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A permission group with this name already exists.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        permission_keys = validated_data.pop("permission_keys", [])
        group = PermissionGroup.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            GroupPermission.objects.bulk_create(
                [GroupPermission(group=group, permission=perm) for perm in perms]
            )

        return group

    @transaction.atomic
    def update(self, instance, validated_data):
        permission_keys = validated_data.pop("permission_keys", None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()

        if permission_keys is not None:
            GroupPermission.objects.filter(group=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            GroupPermission.objects.bulk_create(
                [GroupPermission(group=instance, permission=perm) for perm in perms]
            )

            # Any role (school or platform) attached to this group now has a
            # changed effective permission set, so bump their versions to
            # invalidate caches downstream.
            attached_role_ids = SchoolRoleGroup.objects.filter(
                group=instance
            ).values_list("role_id", flat=True)
            for role in SchoolRoleTemplate.objects.filter(id__in=list(attached_role_ids)):
                role.bump_version()
                role.save(update_fields=["version", "updated_at"])

            attached_platform_role_ids = PlatformRoleGroup.objects.filter(
                group=instance
            ).values_list("role_id", flat=True)
            for role in PlatformRoleTemplate.objects.filter(
                id__in=list(attached_platform_role_ids)
            ):
                role.bump_version()
                role.save(update_fields=["version", "updated_at"])

        return instance


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
            from .validators import validate_role_permissions
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
                from .validators import validate_role_permissions
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
            from .validators import validate_role_permissions
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
                from .validators import validate_role_permissions
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

    class Meta:
        model = PlatformUserRoleAssignment
        fields = [
            "id",
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