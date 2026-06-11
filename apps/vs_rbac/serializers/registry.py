"""Permission registry serializers: modules, resources, actions, permissions, dependencies, groups.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers

from ..models import (
    GroupPermission,
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    PlatformRoleGroup,
    PlatformRoleTemplate,
    SchoolRoleGroup,
    SchoolRoleTemplate,
)



# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
class SchoolField(serializers.PrimaryKeyRelatedField):
    """School reference that accepts the surrogate id OR the slug (B23).

    Clients addressed schools by slug while the slug was the primary key;
    this keeps that contract: writes take either form, reads render the slug
    so the wire format is unchanged.
    """

    def get_queryset(self):
        from vs_schools.models import School

        return School.objects.all()

    def to_internal_value(self, data):
        from vs_schools.models import School

        if isinstance(data, School):
            return data
        qs = self.get_queryset()
        try:
            if str(data).isdigit():
                return qs.get(pk=data)
            return qs.get(slug=data)
        except School.DoesNotExist:
            self.fail("does_not_exist", pk_value=data)

    def to_representation(self, value):
        from vs_schools.models import School

        if isinstance(value, School):
            return value.slug
        # PKOnlyObject fast path — fetch the slug.
        return School.objects.filter(pk=value.pk).values_list("slug", flat=True).first()


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
    permissions_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PermissionResource
        fields = ["id", "module", "name", "description", "is_active", "permissions_count", "created_at", "updated_at"]
        read_only_fields = ["id", "permissions_count", "created_at", "updated_at"]

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
    permissions_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PermissionAction
        fields = ["name", "description", "is_active", "permissions_count", "created_at", "updated_at"]
        read_only_fields = ["permissions_count", "created_at", "updated_at"]


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


