"""Unified tenant-scoped RBAC serializers: roles, assignments, change requests.

These operate exclusively on the canonical tenant RBAC tables
(``TenantRoleTemplate`` / ``TenantRolePermission`` / ``TenantRoleGroup`` /
``TenantUserRoleAssignment`` / ``TenantRoleChangeRequest``).

Scope (tenant / branch) never comes from the request body — it is injected by
the view from the URL / ``request.tenant``. Serializers validate that any
referenced user, role, or branch belongs to that tenant.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from vs_schools.models import Branch

from ..models import (
    Permission,
    PermissionGroup,
    TenantRoleChangeDeltaItem,
    TenantRoleChangeRequest,
    TenantRoleGroup,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from .registry import (
    PermissionGroupListSerializer,
    PermissionKeyListValidationMixin,
    PermissionSerializer,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _unique_tenant_role_key(tenant, name, exclude_pk=None) -> str:
    """Build a slug key unique within *tenant* (roles are addressed by key)."""
    base = slugify(name) or "role"
    slug = base
    n = 1
    while True:
        qs = TenantRoleTemplate.objects.filter(tenant=tenant, key=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return slug
        slug = f"{base}-{n}"
        n += 1


# -----------------------------------------------------------------------------
# Role templates + role permissions
# -----------------------------------------------------------------------------
class TenantRolePermissionSerializer(serializers.ModelSerializer):
    """One permission row attached to a tenant role template."""

    permission_key = serializers.CharField(source="permission.key", read_only=True)

    class Meta:
        model = TenantRolePermission
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


class TenantRoleGroupAttachmentSerializer(serializers.ModelSerializer):
    """Read-only view of a permission group attached to a tenant role."""

    group = PermissionGroupListSerializer(read_only=True)

    class Meta:
        model = TenantRoleGroup
        fields = [
            "id",
            "group",
            "attached_by",
            "attached_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class TenantRoleTemplateListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for role list screens."""

    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    assigned_users_count = serializers.IntegerField(read_only=True)
    permissions_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TenantRoleTemplate
        fields = [
            "id",
            "key",
            "tenant",
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
        read_only_fields = fields


class TenantRoleTemplateDetailSerializer(
    PermissionKeyListValidationMixin, serializers.ModelSerializer
):
    """Detailed serializer for tenant role templates.

    Write:
    - ``permission_keys`` replaces the role's direct permission rows.
    - ``group_ids`` replaces the role's attached permission groups.
    - dependency validation runs against the flattened effective set.

    Scope (tenant) is injected by the view; ``branch`` (when supplied) must
    belong to the tenant.
    """

    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    role_permissions = TenantRolePermissionSerializer(many=True, read_only=True)
    role_groups = TenantRoleGroupAttachmentSerializer(many=True, read_only=True)

    permission_keys = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of permission keys to grant to this role.",
    )
    group_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of permission group ids to attach to this role.",
    )

    class Meta:
        model = TenantRoleTemplate
        fields = [
            "id",
            "key",
            "tenant",
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
            "id",
            "key",
            "is_system_role",
            "version",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def _tenant(self):
        tenant = self.context.get("tenant")
        if tenant is None and self.instance is not None:
            tenant = self.instance.tenant
        return tenant

    def validate_branch(self, value):
        if value is None:
            return value
        tenant = self._tenant()
        if tenant is not None and value.school.tenant_id != tenant.pk:
            raise serializers.ValidationError("Branch must belong to this tenant.")
        return value

    def validate(self, attrs):
        tenant = self._tenant()
        if tenant is None:
            raise serializers.ValidationError({"tenant": "Tenant context is required."})
        name = attrs.get("name") or getattr(self.instance, "name", None)
        if name:
            qs = TenantRoleTemplate.objects.filter(tenant=tenant, name__iexact=name)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"name": "A role with this name already exists in this tenant."}
                )
        return attrs

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
            raise serializers.ValidationError(f"Unknown permission group ids: {missing}")
        return cleaned

    @transaction.atomic
    def create(self, validated_data):
        permission_keys = validated_data.pop("permission_keys", [])
        group_ids = validated_data.pop("group_ids", [])

        if permission_keys or group_ids:
            from ..validators import validate_role_permissions

            validate_role_permissions(
                permission_keys=permission_keys, group_ids=group_ids,
            )

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        tenant = self._tenant()

        validated_data["tenant"] = tenant
        validated_data["created_by"] = actor
        validated_data["key"] = _unique_tenant_role_key(tenant, validated_data["name"])
        role = TenantRoleTemplate.objects.create(**validated_data)

        if permission_keys:
            perms = Permission.objects.filter(key__in=permission_keys)
            TenantRolePermission.objects.bulk_create(
                [
                    TenantRolePermission(
                        role=role, permission=perm, granted=True, granted_by=actor,
                    )
                    for perm in perms
                ]
            )

        if group_ids:
            groups = PermissionGroup.objects.filter(id__in=group_ids)
            TenantRoleGroup.objects.bulk_create(
                [
                    TenantRoleGroup(role=role, group=group, attached_by=actor)
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
                    TenantRolePermission.objects.filter(
                        role=instance, granted=True
                    ).values_list("permission_id", flat=True)
                )
            )
            effective_group_ids = (
                group_ids
                if group_ids is not None
                else list(
                    TenantRoleGroup.objects.filter(role=instance).values_list(
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
            instance.version = (instance.version or 1) + 1

        instance.save()

        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if permission_keys is not None:
            TenantRolePermission.objects.filter(role=instance).delete()
            perms = Permission.objects.filter(key__in=permission_keys)
            TenantRolePermission.objects.bulk_create(
                [
                    TenantRolePermission(
                        role=instance, permission=perm, granted=True, granted_by=actor,
                    )
                    for perm in perms
                ]
            )

        if group_ids is not None:
            TenantRoleGroup.objects.filter(role=instance).delete()
            groups = PermissionGroup.objects.filter(id__in=group_ids)
            TenantRoleGroup.objects.bulk_create(
                [
                    TenantRoleGroup(role=instance, group=group, attached_by=actor)
                    for group in groups
                ]
            )

        return instance


# -----------------------------------------------------------------------------
# User role assignments
# -----------------------------------------------------------------------------
class TenantUserRoleAssignmentSerializer(serializers.ModelSerializer):
    """Assign or revoke a tenant role for a user.

    Rules enforced:
    - the user must belong to the assignment tenant
    - the role must belong to the assignment tenant
    - the branch (when set) must belong to the tenant
    """

    user = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), write_only=True,
    )
    role = serializers.PrimaryKeyRelatedField(
        queryset=TenantRoleTemplate.objects.all(), write_only=True,
    )
    branch = serializers.PrimaryKeyRelatedField(
        queryset=Branch.objects.all(), required=False, allow_null=True,
    )

    user_id = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    role_id = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)

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

    class Meta:
        model = TenantUserRoleAssignment
        fields = [
            "id",
            "tenant",
            "user",
            "role",
            "branch",
            "user_id",
            "user_name",
            "user_email",
            "role_id",
            "role_name",
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
            "user_id",
            "user_name",
            "user_email",
            "role_id",
            "role_name",
            "assigned_by",
            "assigned_at",
            "revoked_at",
            "revoked_by",
            "created_at",
            "updated_at",
        ]

    def _tenant(self):
        tenant = self.context.get("tenant")
        if tenant is None and self.instance is not None:
            tenant = self.instance.tenant
        return tenant

    def validate(self, attrs):
        tenant = self._tenant()
        if tenant is None:
            raise serializers.ValidationError({"tenant": "Tenant context is required."})

        user = attrs.get("user") or getattr(self.instance, "user", None)
        role = attrs.get("role") or getattr(self.instance, "role", None)
        branch = attrs.get("branch") if "branch" in attrs else getattr(self.instance, "branch", None)
        new_status = attrs.get(
            "assignment_status",
            getattr(
                self.instance,
                "assignment_status",
                TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ),
        )

        if user is not None and getattr(user, "tenant_id", None) != tenant.pk:
            raise serializers.ValidationError(
                {"user": "User must belong to the same tenant as the assignment."}
            )
        if role is not None and role.tenant_id != tenant.pk:
            raise serializers.ValidationError(
                {"role": "Role must belong to the same tenant as the assignment."}
            )
        if branch is not None and branch.school.tenant_id != tenant.pk:
            raise serializers.ValidationError(
                {"branch": "Branch must belong to the same tenant as the assignment."}
            )

        if (
            new_status == TenantUserRoleAssignment.AssignmentStatus.ACTIVE
            and user and role
        ):
            qs = TenantUserRoleAssignment.objects.filter(
                tenant=tenant,
                user=user,
                role=role,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"role": "This user already has an active assignment for this role."}
                )

        if (
            self.instance
            and self.instance.assignment_status
            == TenantUserRoleAssignment.AssignmentStatus.REVOKED
            and new_status == TenantUserRoleAssignment.AssignmentStatus.ACTIVE
        ):
            raise serializers.ValidationError(
                {
                    "assignment_status": (
                        "A revoked assignment cannot be reactivated. "
                        "Create a new assignment instead."
                    )
                }
            )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None
        validated_data["tenant"] = self._tenant()
        validated_data["assigned_by"] = actor
        return super().create(validated_data)

    def update(self, instance, validated_data):
        new_status = validated_data.get("assignment_status", instance.assignment_status)
        request = self.context.get("request")
        actor = request.user if request and request.user.is_authenticated else None

        if (
            new_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED
            and instance.assignment_status
            != TenantUserRoleAssignment.AssignmentStatus.REVOKED
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
# Role change requests
# -----------------------------------------------------------------------------
class TenantRoleChangeDeltaItemSerializer(serializers.ModelSerializer):
    """One requested change item: ADD or REMOVE a permission."""

    permission_key = serializers.CharField(write_only=True)
    permission = PermissionSerializer(read_only=True)

    class Meta:
        model = TenantRoleChangeDeltaItem
        fields = [
            "id",
            "permission_key",
            "permission",
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
            raise serializers.ValidationError("Unknown permission_key.")
        return value


class TenantRoleChangeRequestSerializer(serializers.ModelSerializer):
    """Create a tenant role change request with delta items."""

    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    delta_items = TenantRoleChangeDeltaItemSerializer(many=True)

    class Meta:
        model = TenantRoleChangeRequest
        fields = [
            "id",
            "tenant",
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

    def _tenant(self):
        tenant = self.context.get("tenant")
        if tenant is None and self.instance is not None:
            tenant = self.instance.tenant
        return tenant

    def validate(self, attrs):
        tenant = self._tenant()
        if tenant is None:
            raise serializers.ValidationError({"tenant": "Tenant context is required."})

        target_role = attrs.get("target_role") or getattr(
            self.instance, "target_role", None
        )
        if target_role is not None and target_role.tenant_id != tenant.pk:
            raise serializers.ValidationError(
                {"target_role": "Target role must belong to the same tenant as the request."}
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

        validated_data["tenant"] = self._tenant()
        validated_data["requested_by"] = actor
        obj = TenantRoleChangeRequest.objects.create(**validated_data)

        for item in delta_items_data:
            permission_key = item.pop("permission_key")
            perm = Permission.objects.get(key=permission_key)
            TenantRoleChangeDeltaItem.objects.create(
                request=obj, permission=perm, **item,
            )

        return obj
