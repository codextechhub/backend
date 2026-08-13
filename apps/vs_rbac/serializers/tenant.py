"""Unified tenant-scoped RBAC serializers: roles, assignments, change requests.

These operate exclusively on the canonical tenant RBAC tables
(``TenantRoleTemplate`` / ``TenantRolePermission`` / ``TenantRoleGroup`` /
``TenantUserRoleAssignment`` / ``TenantRoleChangeRequest``).

Scope (tenant / branch) never comes from the request body - it is injected by
the view from the URL / ``request.tenant``. Every referenced user, role or
branch is *resolved inside* that tenant, so a reference the caller is not
entitled to is simply not found rather than found-then-rejected.
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
from ..services import SUPER_ADMIN_ROLE_KEY
from .registry import (
    PermissionGroupListSerializer,
    PermissionKeyListValidationMixin,
    PermissionSerializer,
)


# -----------------------------------------------------------------------------
# Tenant-scoped reference resolution
# -----------------------------------------------------------------------------
# Resolving a reference globally and *then* comparing tenants rejects correctly
# but tells the caller the difference between "this id is not yours" and "this
# id does not exist" - an existence oracle it can enumerate with. Every
# reference below is therefore resolved INSIDE the tenant, so a foreign row, an
# absent row and an unusable id all take the same code path and come back with
# the same message. ``vs_schools.services.references`` sets the same standard
# for branch references on the school side; the wording is kept in step
# deliberately, but the constant is local because ``vs_rbac`` is a
# domain-neutral engine app and must not grow a dependency on ``vs_schools``
# beyond the model import it already has.

# The largest value a 64-bit signed column can hold. Every pk resolved here is
# a BigAutoField, and PostgreSQL raises (a 500) rather than returning no rows
# when handed something larger, so oversized ids are "not found" too.
_MAX_BIGINT = 9_223_372_036_854_775_807

BRANCH_NOT_FOUND = "No such branch in this tenant."
USER_NOT_FOUND = "No such user in this tenant."
ROLE_NOT_FOUND = "No such role in this tenant."


class TenantScopedSerializerMixin:
    """Supplies the tenant that every reference on the serializer resolves in.

    The tenant is injected by the view (``TenantScopedRBACMixin`` puts it in the
    serializer context); on an update it can also be read off the instance being
    edited, whose tenant is fixed and never writable.
    """

    def _tenant(self):
        tenant = self.context.get("tenant")
        if tenant is None and self.instance is not None:
            tenant = getattr(self.instance, "tenant", None)
        return tenant

    def run_validation(self, data=serializers.empty):
        """Refuse the payload before a single reference is resolved.

        With no tenant nothing can be scoped, so nothing may be looked up:
        resolving first would let a caller in this state still tell an id that
        exists somewhere from one that exists nowhere. Every view supplies the
        tenant, so this only guards non-HTTP callers - but it is what makes the
        guarantee unconditional rather than "unless the context is missing".
        """
        if self._tenant() is None:
            # List-wrapped to match the shape ``validate`` produces once DRF has
            # normalised it, so the envelope is the same whichever path fires.
            raise serializers.ValidationError(
                {"tenant": ["Tenant context is required."]}
            )
        return super().run_validation(data)


class TenantScopedRelatedField(serializers.PrimaryKeyRelatedField):
    """A pk reference resolved inside the serializer's tenant, or not at all.

    ``tenant_lookup`` is the ORM path from the referenced model to the tenant
    (``"tenant"``, ``"entity__tenant"``, ...). ``not_found`` replaces *both*
    DRF failure messages, so an id that is not a plausible bigint, an id that
    does not exist and an id owned by another tenant are indistinguishable.
    """

    def __init__(self, *, tenant_lookup, not_found, **kwargs):
        self.tenant_lookup = tenant_lookup
        self.not_found = not_found
        error_messages = dict(kwargs.pop("error_messages", None) or {})
        error_messages.setdefault("does_not_exist", not_found)
        error_messages.setdefault("incorrect_type", not_found)
        super().__init__(error_messages=error_messages, **kwargs)

    def _tenant(self):
        """Walk up to the serializer that knows the tenant, if there is one."""
        parent = self.parent
        while parent is not None and not hasattr(parent, "_tenant"):
            parent = parent.parent
        return parent._tenant() if parent is not None else None

    def get_queryset(self):
        queryset = super().get_queryset()
        tenant = self._tenant()
        if tenant is None:
            # The tenant is not knowable at field time (an unusual binding, or
            # a serializer built with neither context nor instance). Resolving
            # against an empty queryset would break legitimate flows, so
            # resolve unscoped and let the serializer's fallback check reject
            # with the identical message - the oracle survives neither path.
            return queryset
        return queryset.filter(**{self.tenant_lookup: tenant})

    def to_internal_value(self, data):
        # A non-numeric or oversized id is "not found", never a database error.
        raw = str(data).strip()
        if not raw.isdigit() or int(raw) > _MAX_BIGINT:
            self.fail("does_not_exist", pk_value=data)
        return super().to_internal_value(data)


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
    TenantScopedSerializerMixin,
    PermissionKeyListValidationMixin,
    serializers.ModelSerializer,
):
    """Detailed serializer for tenant role templates.

    Write:
    - ``permission_keys`` replaces the role's direct permission rows.
    - ``group_ids`` replaces the role's attached permission groups.
    - dependency validation runs against the flattened effective set.

    Scope (tenant) is injected by the view; ``branch`` (when supplied) is
    resolved inside that tenant, so another tenant's branch is simply not
    found.
    """

    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    # all_objects + an explicit filter deliberately: the tenant the serializer
    # was given is the security boundary, and it must not depend on the ambient
    # request-local tenant state that ``Branch.objects`` reads.
    branch = TenantScopedRelatedField(
        queryset=Branch.all_objects.all(),
        tenant_lookup="tenant",
        not_found=BRANCH_NOT_FOUND,
        required=False,
        allow_null=True,
    )

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

    def validate_branch(self, value):
        """Fallback tenancy check for a branch the field resolved unscoped.

        ``branch`` is normally resolved inside the tenant by the field itself,
        which makes this unreachable. It stays as the backstop for the one case
        the field cannot cover - it could not reach a tenant through its parent
        - and raises the *same* message the lookup does so that path is not an
        oracle either. When no tenant is knowable at all the mixin's
        ``run_validation`` has already refused the payload, so nothing was
        looked up in the first place.
        """
        if value is None:
            return value
        tenant = self._tenant()
        if tenant is not None and value.tenant_id != tenant.pk:
            raise serializers.ValidationError(BRANCH_NOT_FOUND)
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
class TenantUserRoleAssignmentSerializer(
    TenantScopedSerializerMixin, serializers.ModelSerializer,
):
    """Assign or revoke a tenant role for a user.

    Every reference is resolved inside the assignment's tenant, so a user, role
    or branch belonging to another tenant is reported exactly like one that does
    not exist. Nothing is ever accepted across a tenant boundary.
    """

    user = TenantScopedRelatedField(
        queryset=get_user_model().objects.all(),
        tenant_lookup="tenant",
        not_found=USER_NOT_FOUND,
        write_only=True,
    )
    role = TenantScopedRelatedField(
        queryset=TenantRoleTemplate.objects.all(),
        tenant_lookup="tenant",
        not_found=ROLE_NOT_FOUND,
        write_only=True,
    )
    # all_objects + an explicit filter deliberately: see the role template
    # serializer above - ambient tenant state must not be the boundary.
    branch = TenantScopedRelatedField(
        queryset=Branch.all_objects.all(),
        tenant_lookup="tenant",
        not_found=BRANCH_NOT_FOUND,
        required=False,
        allow_null=True,
    )

    user_id = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    role_id = serializers.SerializerMethodField()
    role_key = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    assigned_by_id = serializers.SerializerMethodField()
    assigned_by_name = serializers.SerializerMethodField()
    revoked_by_id = serializers.SerializerMethodField()
    revoked_by_name = serializers.SerializerMethodField()
    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    def get_user_id(self, obj):
        return str(obj.user_id) if obj.user_id else None

    def get_user_name(self, obj):
        return getattr(obj.user, "full_name", None) or getattr(obj.user, "email", None)

    def get_user_email(self, obj):
        return getattr(obj.user, "email", None)

    def get_role_id(self, obj):
        return str(obj.role_id) if obj.role_id else None

    def get_role_key(self, obj):
        return getattr(obj.role, "key", None)

    def get_role_name(self, obj):
        return getattr(obj.role, "name", None)

    def get_assigned_by_id(self, obj):
        return str(obj.assigned_by_id) if obj.assigned_by_id else None

    def get_assigned_by_name(self, obj):
        if not obj.assigned_by_id:
            return None
        return (
            getattr(obj.assigned_by, "full_name", None)
            or getattr(obj.assigned_by, "email", None)
        )

    def get_revoked_by_id(self, obj):
        return str(obj.revoked_by_id) if obj.revoked_by_id else None

    def get_revoked_by_name(self, obj):
        if not obj.revoked_by_id:
            return None
        return (
            getattr(obj.revoked_by, "full_name", None)
            or getattr(obj.revoked_by, "email", None)
        )

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
            "role_key",
            "role_name",
            "assignment_status",
            "assigned_by",
            "assigned_by_id",
            "assigned_by_name",
            "assigned_at",
            "revoked_at",
            "revoked_by",
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
            "role_key",
            "role_name",
            "assigned_by",
            "assigned_by_id",
            "assigned_by_name",
            "assigned_at",
            "revoked_at",
            "revoked_by",
            "revoked_by_id",
            "revoked_by_name",
            "created_at",
            "updated_at",
        ]

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

        # Fallback tenancy checks. The three references are resolved inside the
        # tenant by their fields, so these are unreachable through the API; they
        # remain as the backstop for a field that could not reach a tenant, and
        # each raises the same message its lookup does so neither route reveals
        # that the id exists somewhere else.
        if user is not None and getattr(user, "tenant_id", None) != tenant.pk:
            raise serializers.ValidationError({"user": USER_NOT_FOUND})
        if role is not None and role.tenant_id != tenant.pk:
            raise serializers.ValidationError({"role": ROLE_NOT_FOUND})
        if branch is not None and branch.tenant_id != tenant.pk:
            raise serializers.ValidationError({"branch": BRANCH_NOT_FOUND})

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

        is_assigning_super_admin = (
            role
            and role.key == SUPER_ADMIN_ROLE_KEY
            and (self.instance is None or self.instance.role_id != role.id)
        )
        if is_assigning_super_admin:
            raise serializers.ValidationError(
                {"role": "Use Transfer Super Admin to assign the Super Admin role."}
            )

        is_removing_super_admin = (
            self.instance
            and self.instance.role.key == SUPER_ADMIN_ROLE_KEY
            and self.instance.assignment_status
            == TenantUserRoleAssignment.AssignmentStatus.ACTIVE
            and new_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED
        )
        if is_removing_super_admin:
            raise serializers.ValidationError(
                {
                    "assignment_status": (
                        "Transfer Super Admin before revoking this assignment."
                    )
                }
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


class TenantRoleChangeRequestSerializer(
    TenantScopedSerializerMixin, serializers.ModelSerializer,
):
    """Create a tenant role change request with delta items.

    ``target_role`` is resolved inside the request's tenant, so another
    tenant's role is reported exactly like a role that does not exist.
    """

    tenant = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    target_role = TenantScopedRelatedField(
        queryset=TenantRoleTemplate.objects.all(),
        tenant_lookup="tenant",
        not_found=ROLE_NOT_FOUND,
    )
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

    def validate(self, attrs):
        tenant = self._tenant()
        if tenant is None:
            raise serializers.ValidationError({"tenant": "Tenant context is required."})

        target_role = attrs.get("target_role") or getattr(
            self.instance, "target_role", None
        )
        # Fallback tenancy check - see the assignment serializer above. Same
        # message as the lookup, so a foreign role stays indistinguishable from
        # an absent one on this route too.
        if target_role is not None and target_role.tenant_id != tenant.pk:
            raise serializers.ValidationError({"target_role": ROLE_NOT_FOUND})
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
