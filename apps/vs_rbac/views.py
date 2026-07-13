from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from .models import (
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    TenantRoleChangeRequest,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from .serializers import (
    PermissionActionSerializer,
    PermissionDependencySerializer,
    PermissionDetailSerializer,
    PermissionGroupDetailSerializer,
    PermissionGroupListSerializer,
    PermissionModuleSerializer,
    PermissionResourceSerializer,
    PermissionSerializer,
    TenantRoleChangeRequestSerializer,
    TenantRoleTemplateDetailSerializer,
    TenantRoleTemplateListSerializer,
    TenantUserRoleAssignmentSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsVisionSuperAdmin,
    HasRBACPermission,
    is_vision_super_admin,
)


# -----------------------------------------------------------------------------
# Tenant-scoped RBAC — shared plumbing
# -----------------------------------------------------------------------------
# Permission keys per operation are any-of lists spanning the school-side
# (``school.roles.*``) and platform-side (``platform.roles.*``) vocabularies so
# both already-migrated grants keep working on the unified endpoint. The Vision
# super admin bypasses these checks via HasRBACPermission.
ROLE_VIEW_KEYS = ["school.roles.view", "platform.roles.view"]
ROLE_CREATE_KEYS = ["school.roles.create", "platform.roles.create"]
ROLE_UPDATE_KEYS = ["school.roles.update", "platform.roles.update"]
ROLE_DELETE_KEYS = ["school.roles.delete", "platform.roles.delete"]
ROLE_ASSIGN_KEYS = ["school.roles.assign", "platform.roles.assign"]


class TenantScopedRBACMixin:
    """Bind + validate the URL tenant slug against the authenticated tenant.

    ``request.tenant`` is established by ``TenantJWTAuthentication`` (which also
    enforces that the caller may assert that tenant). This mixin adds the
    non-enumerating guard that the URL ``tenant_slug`` matches the bound tenant,
    so a caller cannot reach another tenant's rows by changing the path.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Runs after authentication + permission checks; request.tenant is set.
        self.tenant = self.get_tenant()

    def get_tenant(self):
        slug = self.kwargs.get("tenant_slug")
        tenant = getattr(self.request, "tenant", None)
        if tenant is None or tenant.slug != slug:
            raise NotFound("No tenant matches the requested context.")
        return tenant

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["tenant"] = getattr(self, "tenant", None) or self.get_tenant()
        return context


# -----------------------------------------------------------------------------
# Permission vocabulary — Module / Resource / Action (Vision-owned)
# -----------------------------------------------------------------------------

# List and create permission modules in the Vision-owned vocabulary.
class PermissionModuleListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission modules"""
    queryset = PermissionModule.objects.all()
    serializer_class = PermissionModuleSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        # Creating vocabulary is stricter than reading it.
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one permission module by its stable name.
class PermissionModuleDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission modules"""
    queryset = PermissionModule.objects.all()
    serializer_class = PermissionModuleSerializer
    lookup_field = "name"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# List and create resources within a permission module.
class PermissionResourceListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission resources"""
    queryset = PermissionResource.objects.select_related("module").all()
    serializer_class = PermissionResourceSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset().annotate(permissions_count=Count("permissions", distinct=True))
        qp = self.request.query_params
        if module := qp.get("module"):
            qs = qs.filter(module_id=module)
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one permission resource.
class PermissionResourceDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission resources"""
    queryset = PermissionResource.objects.select_related("module").annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionResourceSerializer

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# List and create action verbs used when composing permission keys.
class PermissionActionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission actions"""
    queryset = PermissionAction.objects.all()
    serializer_class = PermissionActionSerializer
    pagination_class = XVSPagination

    def get_queryset(self):
        qs = super().get_queryset().annotate(permissions_count=Count("permissions", distinct=True))
        qp = self.request.query_params
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if search := qp.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or mutate one action verb by name.
class PermissionActionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permission actions"""
    queryset = PermissionAction.objects.annotate(permissions_count=Count("permissions", distinct=True))
    serializer_class = PermissionActionSerializer
    lookup_field = "name"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# -----------------------------------------------------------------------------
# Global Permission Registry (Vision-owned)
# -----------------------------------------------------------------------------
# List and create concrete permission keys from module/resource/action vocabulary.
class PermissionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permissions"""
    queryset = Permission.objects.select_related("module", "resource", "action").order_by("-updated_at", "module", "action", "key")
    serializer_class = PermissionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        # Registry writes use create rights; list views require only read access.
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = super().get_queryset()
        qp = self.request.query_params

        if module_key := qp.get("module_key"):
            qs = qs.filter(module_id=module_key)
        if action_key := qp.get("action"):
            qs = qs.filter(action_id=action_key)
        if is_active := qp.get("is_active"):
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)
        if is_restricted := qp.get("is_restricted"):
            lowered = is_restricted.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_restricted=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_restricted=False)
        if sensitivity_level := qp.get("sensitivity_level"):
            qs = qs.filter(sensitivity_level=sensitivity_level)
        if search := qp.get("search"):
            qs = qs.filter(
                Q(key__icontains=search) |
                Q(module__name__icontains=search) |
                Q(resource__name__icontains=search) |
                Q(action__name__icontains=search) |
                Q(description__icontains=search)
            )

        return qs


# Retrieve, update, or delete one concrete permission key.
class PermissionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """docstring-name: Permissions"""
    queryset = Permission.objects.prefetch_related(
        "groups", "dependencies__depends_on", "required_by__permission"
    ).all()
    lookup_field = "key"

    def get_serializer_class(self):
        # Detail reads include dependencies and group membership; writes use the lean serializer.
        if self.request.method == "GET":
            return PermissionDetailSerializer
        return PermissionSerializer

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.delete"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.permissions.update"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        try:
            instance = self.get_object()
        except Exception:
            return error_response(
                message="Permission not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        if not serializer.is_valid():
            return error_response(
                message="Invalid data.",
                error={"errors": serializer.errors},
            )

        try:
            validated = serializer.validated_data

            # Auto-compute new key from whatever module/resource/action ended up in
            # validated_data (new value if sent, existing instance value otherwise).
            # key is read-only in the serializer so we handle the PK update here.
            new_module = validated.get("module", instance.module)
            new_resource = validated.get("resource", instance.resource)
            new_action = validated.get("action", instance.action)
            new_key = f"{new_module.pk}.{new_resource.name}.{new_action.pk}"

            if new_key != instance.key:
                # Updating module/resource/action changes the natural key used by role grants.
                Permission.objects.filter(key=instance.key).update(key=new_key)
                instance.key = new_key

            self.perform_update(serializer)
        except Exception as exc:
            return error_response(
                message="Update failed.",
                error={"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return success_response(
            message="Permission updated successfully.",
            data=serializer.data,
        )

    def delete(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
        except Exception:
            return error_response(
                message="Permission not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            self.perform_destroy(instance)
        except Exception as exc:
            return error_response(
                message="Delete failed.",
                error={"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return success_response(message="Permission deleted successfully.")


# List and create dependency rules between permission keys.
class PermissionDependencyListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """docstring-name: Permission dependencies"""
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# Retrieve or remove one dependency rule.
class PermissionDependencyDetailView(RetrieveModelMixin, DestroyModelMixin, generics.RetrieveDestroyAPIView):
    """docstring-name: Permission dependencies"""
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


# -----------------------------------------------------------------------------
# Permission Groups (Vision-owned, shared across school + platform roles)
# -----------------------------------------------------------------------------
# List and create reusable permission bundles.
class PermissionGroupListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list all permission groups
    - POST: create a new permission group with optional permission_keys

    docstring-name: Permission groups
    """
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PermissionGroup.objects.all()
            .annotate(permissions_count=Count("group_permissions", distinct=True))
            .order_by("name")
        )

        is_active = self.request.query_params.get("is_active")
        is_system = self.request.query_params.get("is_system")

        if is_active is not None:
            lowered = is_active.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_active=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_active=False)

        if is_system is not None:
            lowered = is_system.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_system=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_system=False)
        if search := self.request.query_params.get("search"):
            qs = qs.filter(Q(name__icontains=search))

        return qs

    def get_serializer_class(self):
        # Create accepts permission_keys, while list keeps the payload compact.
        if self.request.method == "POST":
            return PermissionGroupDetailSerializer
        return PermissionGroupListSerializer


# Retrieve, update, or delete one permission bundle.
class PermissionGroupDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: group detail with expanded permissions
    - PATCH/PUT: update group fields and optionally replace permission_keys
    - DELETE: blocked for system groups

    docstring-name: Permission groups
    """
    serializer_class = PermissionGroupDetailSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH", "DELETE"):
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        return PermissionGroup.objects.all().prefetch_related("permissions")

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system:
            # System bundles may back shipped roles, so they are not user-deletable.
            return error_response(
                message="System permission groups cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# Tenant-scoped Role Templates
# -----------------------------------------------------------------------------
# List and create role templates inside one tenant boundary.
class TenantRoleTemplateListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list role templates in a tenant
    - POST: create a role template in a tenant

    docstring-name: Roles
    """
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_CREATE_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qp = self.request.query_params
        qs = (
            TenantRoleTemplate.objects.filter(tenant=tenant)
            .annotate(
                assigned_users_count=Count(
                    "user_assignments",
                    filter=Q(user_assignments__assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE),
                    distinct=True,
                ),
                permissions_count=Count(
                    "role_permissions",
                    filter=Q(role_permissions__granted=True),
                    distinct=True,
                ),
            )
            .select_related("created_by", "tenant", "branch")
            .order_by("name")
        )
        if branch_id := qp.get("branch"):
            qs = qs.filter(branch_id=branch_id)
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TenantRoleTemplateDetailSerializer
        return TenantRoleTemplateListSerializer


# Retrieve or mutate one tenant role template (addressed by per-tenant key).
class TenantRoleTemplateDetailView(TenantScopedRBACMixin, RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Tenant-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: blocked for system or locked roles

    docstring-name: Roles
    """
    serializer_class = TenantRoleTemplateDetailSerializer
    lookup_field = "key"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = ROLE_DELETE_KEYS
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = ROLE_UPDATE_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantRoleTemplate.objects.filter(tenant=tenant)
            .select_related("created_by", "tenant", "branch")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_locked:
            return error_response(
                message="This role is locked and cannot be modified.",
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.is_system_role:
            return error_response(
                message="System roles cannot be modified.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_system_role:
            return error_response(
                message="System roles cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.is_locked:
            return error_response(
                message="This role is locked and cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# Tenant-scoped Role Assignments
# -----------------------------------------------------------------------------
# List and create tenant-scoped role assignments.
class TenantUserRoleAssignmentListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list assignments in a tenant
    - POST: assign a role to a user inside a tenant

    docstring-name: Role assignments
    """
    serializer_class = TenantUserRoleAssignmentSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_ASSIGN_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantUserRoleAssignment.objects.filter(tenant=tenant)
            .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
            .order_by("-created_at")
        )
        qp = self.request.query_params
        if user_id := qp.get("user"):
            qs = qs.filter(user_id=user_id)
        if role_id := qp.get("role"):
            qs = qs.filter(role_id=role_id)
        if assignment_status := qp.get("assignment_status"):
            qs = qs.filter(assignment_status=assignment_status)
        return qs


# Retrieve or update one tenant-scoped role assignment.
class TenantUserRoleAssignmentDetailView(TenantScopedRBACMixin, RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    Tenant-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow

    docstring-name: Role assignments
    """
    serializer_class = TenantUserRoleAssignmentSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = ROLE_ASSIGN_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantUserRoleAssignment.objects.filter(tenant=tenant)
            .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
        )


# Revoke a tenant role assignment with an audit reason.
class TenantUserRoleAssignmentRevokeView(TenantScopedRBACMixin, APIView):
    """
    Tenant-facing revoke endpoint for role assignments.

    POST /rbac/tenants/<slug>/role-assignments/<id>/revoke/
    Body: { "reason_note": "Required justification for the audit trail." }

    docstring-name: Revoke a role assignment
    """

    def get_permissions(self):
        self.rbac_permission = ROLE_ASSIGN_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def post(self, request, tenant_slug: str, id: int):
        tenant = self.tenant
        reason = (request.data.get("reason_note") or "").strip()
        if not reason:
            return error_response(
                message="A reason is required to revoke an assignment.",
                error={"reason_note": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            assignment = (
                TenantUserRoleAssignment.objects
                .select_related("user", "role", "assigned_by", "revoked_by", "tenant", "branch")
                .get(id=id, tenant=tenant)
            )
        except TenantUserRoleAssignment.DoesNotExist:
            return error_response(
                message="Assignment not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if assignment.assignment_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED:
            return error_response(
                message="This assignment has already been revoked.",
                status=status.HTTP_409_CONFLICT,
            )

        assignment.revoke(by_user=request.user, reason=reason)
        assignment.save(update_fields=[
            "assignment_status", "revoked_at", "revoked_by", "reason_note", "updated_at",
        ])

        return success_response(
            message="Assignment revoked successfully.",
            data=TenantUserRoleAssignmentSerializer(
                assignment, context={"request": request, "tenant": tenant}
            ).data,
        )


# -----------------------------------------------------------------------------
# Tenant Role Change Requests (tenant-internal approval)
# -----------------------------------------------------------------------------
# List and create tenant-internal role change requests.
class TenantRoleChangeRequestListCreateView(TenantScopedRBACMixin, CreateModelMixin, generics.ListCreateAPIView):
    """
    Tenant-facing:
    - GET: list requests for a tenant
    - POST: create a change request for a role in that tenant

    docstring-name: Role change requests
    """
    serializer_class = TenantRoleChangeRequestSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = ROLE_UPDATE_KEYS
        else:
            self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )
        qp = self.request.query_params
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        if role_id := qp.get("target_role"):
            qs = qs.filter(target_role_id=role_id)
        return qs


# List role change requests that tenant admins can review.
class TenantRoleChangeRequestApprovalQueueView(TenantScopedRBACMixin, generics.ListAPIView):
    """
    Tenant-admin-facing:
    - GET: role change requests for a tenant (filter by ?status=)

    docstring-name: Role change approval queue
    """
    serializer_class = TenantRoleChangeRequestSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        qs = (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )
        qp = self.request.query_params
        if status_q := qp.get("status"):
            qs = qs.filter(status=status_q)
        if target_role := qp.get("target_role"):
            qs = qs.filter(target_role_id=target_role)
        return qs


# Retrieve one tenant role change request for review.
class TenantRoleChangeRequestApprovalDetailView(TenantScopedRBACMixin, RetrieveModelMixin, generics.RetrieveAPIView):
    """
    Tenant-admin-facing:
    - GET: single role change request within the tenant

    docstring-name: Role change approval queue
    """
    serializer_class = TenantRoleChangeRequestSerializer
    lookup_field = "id"

    def get_permissions(self):
        self.rbac_permission = ROLE_VIEW_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        tenant = self.get_tenant()
        return (
            TenantRoleChangeRequest.objects.filter(tenant=tenant)
            .select_related("requested_by", "reviewer", "target_role", "tenant")
            .prefetch_related("delta_items__permission")
        )


# Decide a tenant role change request and apply approved permission deltas.
class TenantRoleChangeRequestDecisionView(TenantScopedRBACMixin, APIView):
    """
    Tenant-admin decision endpoint for role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }

    docstring-name: Decide a role change request
    """

    def get_permissions(self):
        self.rbac_permission = ROLE_UPDATE_KEYS
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def post(self, request, tenant_slug: str, request_id: str):
        tenant = self.tenant
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = TenantRoleChangeRequest.objects.select_related("target_role", "tenant").get(
                id=request_id, tenant=tenant,
            )
        except TenantRoleChangeRequest.DoesNotExist:
            return error_response(
                message="Request not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != TenantRoleChangeRequest.Status.PENDING:
            return error_response(
                message=f"Request already decided ({obj.status}).",
                status=status.HTTP_409_CONFLICT,
            )

        if action == "DENY":
            if not notes:
                return error_response(
                    message="Denial reason is required.",
                    error={"notes": ["Denial reason is required."]},
                )
            obj.mark_denied(reviewer=request.user, notes=notes)
            obj.save(update_fields=[
                "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
            ])
            return success_response(
                message="Role change request denied.",
                data=TenantRoleChangeRequestSerializer(
                    obj, context={"request": request, "tenant": tenant}
                ).data,
            )

        if action == "APPROVE":
            try:
                with transaction.atomic():
                    from .services import apply_role_change_request
                    apply_role_change_request(obj=obj, reviewer=request.user, notes=notes)
            except Exception as exc:
                obj.mark_apply_failed(reviewer=request.user, notes=str(exc))
                obj.save(update_fields=[
                    "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
                ])
                return error_response(
                    message="Approval failed while applying changes.",
                    error={"error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            return success_response(
                message="Role change request approved.",
                data=TenantRoleChangeRequestSerializer(
                    obj, context={"request": request, "tenant": tenant}
                ).data,
            )

        return error_response(
            message="Invalid action. Must be APPROVE or DENY.",
            error={"action": ["Must be APPROVE or DENY."]},
        )


# -----------------------------------------------------------------------------
# Super Admin Transfer (codex tenant)
# -----------------------------------------------------------------------------
# Transfer the singleton Vision super-admin role to another Vision staff user.
class TransferSuperAdminView(APIView):
    """
    POST platform/transfer-super-admin/

    Allows the current Vision Super Admin to transfer their role to another
    Vision Staff member. The caller is demoted to Vision Platform Admin. Operates
    on the codex platform tenant's TenantUserRoleAssignment rows.

    Body: { "new_super_admin_id": "<uuid>" }

    docstring-name: Transfer super admin
    """
    permission_classes = [IsAuthenticatedAndActive, IsVisionSuperAdmin, HasRBACPermission]
    rbac_permission = "platform.roles.transfer"

    def post(self, request):
        from django.conf import settings
        from django.apps import apps
        UserModel = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
        from .services import transfer_super_admin

        new_id = request.data.get("new_super_admin_id")
        if not new_id:
            return error_response(
                message="new_super_admin_id is required.",
                error={"new_super_admin_id": ["This field is required."]},
            )

        try:
            new_user = UserModel.objects.get(pk=new_id)
        except (UserModel.DoesNotExist, Exception):
            return error_response(
                message="User not found.",
                error={"new_super_admin_id": ["No user with this ID exists."]},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            # The service owns demotion, revocation of existing roles, and audit.
            transfer_super_admin(from_user=request.user, to_user=new_user)
        except ValueError as exc:
            return error_response(message=str(exc), error={})

        return success_response(
            message=f"Super admin role transferred to {new_user.email}. You are now a Platform Admin.",
        )
