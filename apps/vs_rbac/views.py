from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.views import APIView

from core.mixins import RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from vs_schools.models import School
from .models import (
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionGroup,
    PermissionModule,
    PermissionResource,
    PlatformRoleChangeRequest,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    SchoolRoleChangeRequest,
    SchoolRoleTemplate,
    SchoolUserRoleAssignment,
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
    PlatformRoleChangeRequestSerializer,
    PlatformRoleTemplateDetailSerializer,
    PlatformRoleTemplateListSerializer,
    PlatformUserRoleAssignmentSerializer,
    SchoolRoleChangeRequestSerializer,
    SchoolRoleTemplateDetailSerializer,
    SchoolRoleTemplateListSerializer,
    SchoolUserRoleAssignmentSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsSchoolAdmin,
    IsVisionSuperAdmin,
    HasRBACPermission,
    is_vision_super_admin,
)


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
# School Role Templates
# -----------------------------------------------------------------------------
# List and create role templates inside one school boundary.
class SchoolRoleTemplateListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list role templates in a school
    - POST: create a role template in a school

    docstring-name: School roles
    """
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qp = self.request.query_params

        # Tenant filtering is URL-driven so school admins cannot list another school's roles.
        qs = (
            SchoolRoleTemplate.objects.filter(school__slug=school_slug)
            .annotate(
                assigned_users_count=Count(
                    "user_assignments",
                    filter=Q(user_assignments__assignment_status=SchoolUserRoleAssignment.AssignmentStatus.ACTIVE),
                    distinct=True,
                ),
                permissions_count=Count(
                    "role_permissions",
                    filter=Q(role_permissions__granted=True),
                    distinct=True,
                ),
            )
            .select_related("created_by", "school", "branch")
            .order_by("name")
        )

        if branch_id := qp.get("branch"):
            qs = qs.filter(branch_id=branch_id)

        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return SchoolRoleTemplateDetailSerializer
        return SchoolRoleTemplateListSerializer

    def perform_create(self, serializer):
        # Bind the new role to the school in the URL, not client-submitted data.
        serializer.save(school=School.objects.get(slug=self.kwargs["school_slug"]))


# Retrieve or mutate one school role template.
class SchoolRoleTemplateDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    School-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: blocked for system or locked roles

    docstring-name: School roles
    """
    serializer_class = SchoolRoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolRoleTemplate.objects.filter(school__slug=school_slug)
            .select_related("created_by", "school")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_locked:
            # Locked roles are provisioned for consistency and cannot be edited locally.
            return error_response(
                message="This role is locked and cannot be modified.",
                status=status.HTTP_403_FORBIDDEN,
            )
        if instance.is_system_role:
            # System roles are Vision-owned even though they are visible inside the school.
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
# School User Role Assignments
# -----------------------------------------------------------------------------
# List and create school-scoped role assignments.
class SchoolUserRoleAssignmentListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list assignments in a school
    - POST: assign a role to a user inside a school

    docstring-name: School role assignments
    """
    serializer_class = SchoolUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]

        qs = (
            SchoolUserRoleAssignment.objects.filter(school__slug=school_slug)
            .select_related("user", "role", "assigned_by", "revoked_by", "school")
            .order_by("-created_at")
        )

        user_id = self.request.query_params.get("user")
        role_id = self.request.query_params.get("role")
        assignment_status = self.request.query_params.get("assignment_status")

        if user_id:
            qs = qs.filter(user_id=user_id)
        if role_id:
            qs = qs.filter(role_id=role_id)
        if assignment_status:
            qs = qs.filter(assignment_status=assignment_status)

        return qs

    def perform_create(self, serializer):
        # Assignment creation is pinned to the school route to prevent cross-school grants.
        serializer.save(school=School.objects.get(slug=self.kwargs["school_slug"]))


# Retrieve or update one school-scoped role assignment.
class SchoolUserRoleAssignmentDetailView(RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    School-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow

    docstring-name: School role assignments
    """
    serializer_class = SchoolUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolUserRoleAssignment.objects.filter(school__slug=school_slug)
            .select_related("user", "role", "assigned_by", "revoked_by", "school")
        )


# -----------------------------------------------------------------------------
# School Role Change Requests (school-internal approval)
# -----------------------------------------------------------------------------
# List and create school-internal role change requests.
class SchoolRoleChangeRequestListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list requests for a school
    - POST: create a change request for a role in that school

    docstring-name: School role change requests
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qs = (
            SchoolRoleChangeRequest.objects.filter(school__slug=school_slug)
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )

        status_q = self.request.query_params.get("status")
        role_id = self.request.query_params.get("target_role")

        if status_q:
            qs = qs.filter(status=status_q)
        if role_id:
            qs = qs.filter(target_role_id=role_id)

        return qs

    def perform_create(self, serializer):
        # The request school is route-owned so approval queues stay tenant-local.
        serializer.save(school=School.objects.get(slug=self.kwargs["school_slug"]))


# List role change requests that school admins can review.
class SchoolRoleChangeRequestApprovalQueueView(generics.ListAPIView):
    """
    School-admin-facing:
    - GET: pending role change requests for a school

    docstring-name: Role change approval queue
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qs = (
            SchoolRoleChangeRequest.objects.filter(school__slug=school_slug)
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )

        status_q = self.request.query_params.get("status")
        target_role = self.request.query_params.get("target_role")

        if status_q:
            qs = qs.filter(status=status_q)
        if target_role:
            qs = qs.filter(target_role_id=target_role)

        return qs


# Retrieve one school role change request for review.
class SchoolRoleChangeRequestApprovalDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    School-admin-facing:
    - GET: single role change request within the school

    docstring-name: Role change approval queue
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolRoleChangeRequest.objects.filter(school__slug=school_slug)
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
        )


# Decide a school role change request and apply approved permission deltas.
class SchoolRoleChangeRequestDecisionView(APIView):
    """
    School-admin decision endpoint for school role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }

    docstring-name: Decide a school role change request
    """
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]

    def post(self, request, school_slug: str, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = SchoolRoleChangeRequest.objects.select_related("target_role", "school").get(
                id=request_id, school__slug=school_slug,
            )
        except SchoolRoleChangeRequest.DoesNotExist:
            return error_response(
                message="Request not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != SchoolRoleChangeRequest.Status.PENDING:
            # A decided request must not be applied or denied twice.
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
            # Denial is a terminal workflow state with reviewer notes preserved for audit.
            obj.save(
                update_fields=[
                    "status",
                    "reviewer",
                    "reviewer_notes",
                    "decided_at",
                    "updated_at",
                ]
            )
            return success_response(
                message="Role change request denied.",
                data=SchoolRoleChangeRequestSerializer(obj, context={"request": request}).data,
            )

        if action == "APPROVE":
            try:
                with transaction.atomic():
                    # The service layer validates dependencies, applies grants, and writes audit.
                    from .services import apply_school_role_change_request
                    apply_school_role_change_request(
                        obj=obj,
                        reviewer=request.user,
                        notes=notes
                    )

            except Exception as exc:
                # Preserve failed approvals as review history instead of leaving them pending.
                obj.mark_apply_failed(reviewer=request.user, notes=str(exc))
                obj.save(
                    update_fields=[
                        "status",
                        "reviewer",
                        "reviewer_notes",
                        "decided_at",
                        "updated_at",
                    ]
                )
                return error_response(
                    message="Approval failed while applying changes.",
                    error={"error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return success_response(
                message="Role change request approved.",
                data=SchoolRoleChangeRequestSerializer(obj, context={"request": request}).data,
            )

        return error_response(
            message="Invalid action. Must be APPROVE or DENY.",
            error={"action": ["Must be APPROVE or DENY."]},
        )


# -----------------------------------------------------------------------------
# Platform Role Templates (Vision/internal)
# -----------------------------------------------------------------------------
# List and create platform-wide role templates.
class PlatformRoleTemplateListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform roles
    - POST: create platform role

    docstring-name: Platform roles
    """
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.roles.create"
        else:
            self.rbac_permission = "platform.roles.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PlatformRoleTemplate.objects.all()
            .annotate(
                assigned_users_count=Count(
                    "user_assignments",
                    filter=Q(user_assignments__assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE),
                    distinct=True,
                ),
                permissions_count=Count(
                    "role_permissions",
                    filter=Q(role_permissions__granted=True),
                    distinct=True,
                ),
            )
            .select_related("created_by")
            .order_by("name")
        )

        status_q = self.request.query_params.get("status")
        is_locked = self.request.query_params.get("is_locked")
        is_system_role = self.request.query_params.get("is_system_role")

        if status_q:
            qs = qs.filter(status=status_q)

        if is_locked is not None:
            lowered = is_locked.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_locked=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_locked=False)

        if is_system_role is not None:
            lowered = is_system_role.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_system_role=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_system_role=False)

        if search := self.request.query_params.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PlatformRoleTemplateDetailSerializer
        return PlatformRoleTemplateListSerializer


# Retrieve or mutate one platform role template.
class PlatformRoleTemplateDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: detail of a platform role
    - PATCH/PUT: blocked for locked or system roles
    - DELETE: blocked for system or locked roles

    docstring-name: Platform roles
    """
    serializer_class = PlatformRoleTemplateDetailSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method == "DELETE":
            self.rbac_permission = "platform.roles.delete"
        elif self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.roles.update"
        else:
            self.rbac_permission = "platform.roles.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        return (
            PlatformRoleTemplate.objects.all()
            .select_related("created_by")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if not is_vision_super_admin(request.user):
            # Only the super admin can override platform lock/system-role protections.
            if instance.is_locked:
                return error_response(
                    message="This platform role is locked and cannot be modified.",
                    status=status.HTTP_403_FORBIDDEN,
                )
            if instance.is_system_role:
                return error_response(
                    message="System platform roles cannot be modified.",
                    status=status.HTTP_403_FORBIDDEN,
                )
        return super().update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        instance = self.get_object()
        if not is_vision_super_admin(request.user):
            # System and locked platform roles are protected from ordinary Vision staff deletion.
            if instance.is_system_role:
                return error_response(
                    message="System platform roles cannot be deleted.",
                    status=status.HTTP_403_FORBIDDEN,
                )
            if instance.is_locked:
                return error_response(
                    message="This platform role is locked and cannot be deleted.",
                    status=status.HTTP_403_FORBIDDEN,
                )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# Platform User Role Assignments
# -----------------------------------------------------------------------------
# List and create platform role assignments for internal users.
class PlatformUserRoleAssignmentListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform user role assignments
    - POST: assign platform role to internal user

    docstring-name: Platform role assignments
    """
    serializer_class = PlatformUserRoleAssignmentSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.roles.assign"
        else:
            self.rbac_permission = "platform.roles.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PlatformUserRoleAssignment.objects.all()
            .select_related("user", "role", "assigned_by", "revoked_by")
            .order_by("-created_at")
        )

        user_id = self.request.query_params.get("user")
        role_id = self.request.query_params.get("role")
        assignment_status = self.request.query_params.get("assignment_status")
        search = self.request.query_params.get("search", "").strip()

        if user_id:
            qs = qs.filter(user_id=user_id)
        if role_id:
            qs = qs.filter(role_id=role_id)
        if assignment_status:
            qs = qs.filter(assignment_status=assignment_status)
        if search:
            from django.db.models import Q as _Q
            qs = qs.filter(
                _Q(user__full_name__icontains=search) |
                _Q(user__email__icontains=search) |
                _Q(role__name__icontains=search)
            )

        return qs

    def perform_create(self, serializer):
        serializer.save()


# Retrieve or update one platform role assignment.
class PlatformUserRoleAssignmentDetailView(RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    Vision-facing:
    - GET: one platform assignment
    - PATCH: often used to revoke

    docstring-name: Platform role assignments
    """
    serializer_class = PlatformUserRoleAssignmentSerializer
    lookup_field = "id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            self.rbac_permission = "platform.roles.assign"
        else:
            self.rbac_permission = "platform.roles.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        return (
            PlatformUserRoleAssignment.objects.all()
            .select_related("user", "role", "assigned_by", "revoked_by")
        )


# Revoke a platform role assignment with an audit reason.
class PlatformUserRoleAssignmentRevokeView(APIView):
    """
    Vision-facing revoke endpoint for platform role assignments.

    POST /rbac/platform/role-assignments/<id>/revoke/
    Body: { "reason_note": "Required justification for the audit trail." }

    docstring-name: Revoke a platform role assignment
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.roles.assign"

    def post(self, request, id: int):
        reason = (request.data.get("reason_note") or "").strip()

        if not reason:
            return error_response(
                message="A reason is required to revoke an assignment.",
                error={"reason_note": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            assignment = (
                PlatformUserRoleAssignment.objects
                .select_related("user", "role", "assigned_by", "revoked_by")
                .get(id=id)
            )
        except PlatformUserRoleAssignment.DoesNotExist:
            return error_response(
                message="Assignment not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if assignment.assignment_status == PlatformUserRoleAssignment.AssignmentStatus.REVOKED:
            # Revocation is terminal; repeated revoke calls should not rewrite audit context.
            return error_response(
                message="This assignment has already been revoked.",
                status=status.HTTP_409_CONFLICT,
            )

        assignment.revoke(by_user=request.user, reason=reason)
        # Save the explicit revocation fields so signals can audit the status change.
        assignment.save(update_fields=[
            "assignment_status",
            "revoked_at",
            "revoked_by",
            "reason_note",
            "updated_at",
        ])

        return success_response(
            message="Assignment revoked successfully.",
            data=PlatformUserRoleAssignmentSerializer(assignment, context={"request": request}).data,
        )


# -----------------------------------------------------------------------------
# Platform Role Change Requests
# -----------------------------------------------------------------------------
# List and create platform role change requests.
class PlatformRoleChangeRequestListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform role change requests
    - POST: create platform role change request

    docstring-name: Platform role change requests
    """
    serializer_class = PlatformRoleChangeRequestSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.roles.update"
        else:
            self.rbac_permission = "platform.roles.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]

    def get_queryset(self):
        qs = (
            PlatformRoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )

        status_q = self.request.query_params.get("status")
        target_role = self.request.query_params.get("target_role")

        if status_q:
            qs = qs.filter(status=status_q)
        if target_role:
            qs = qs.filter(target_role_id=target_role)

        return qs


# Retrieve one platform role change request.
class PlatformRoleChangeRequestDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """docstring-name: Platform role change requests"""
    serializer_class = PlatformRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.roles.view"
    lookup_field = "id"

    def get_queryset(self):
        return (
            PlatformRoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role")
            .prefetch_related("delta_items__permission")
        )


# Decide a platform role change request and apply approved permission deltas.
class PlatformRoleChangeRequestDecisionView(APIView):
    """
    Vision-facing decision endpoint for platform role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }

    docstring-name: Decide a platform role change request
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.roles.update"

    def post(self, request, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = PlatformRoleChangeRequest.objects.select_related("target_role").get(id=request_id)
        except PlatformRoleChangeRequest.DoesNotExist:
            return error_response(
                message="Request not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != PlatformRoleChangeRequest.Status.PENDING:
            # Platform role changes are single-decision requests.
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
            # Denial records reviewer notes and stops the request lifecycle.
            obj.save(
                update_fields=[
                    "status",
                    "reviewer",
                    "reviewer_notes",
                    "decided_at",
                    "updated_at",
                ]
            )
            return success_response(
                message="Platform role change request denied.",
                data=PlatformRoleChangeRequestSerializer(obj, context={"request": request}).data,
            )

        if action == "APPROVE":
            try:
                with transaction.atomic():
                    # The service layer owns dependency validation, grant replacement, and audit.
                    from .services import apply_platform_role_change_request
                    apply_platform_role_change_request(
                        obj=obj,
                        reviewer=request.user,
                        notes=notes,
                    )

            except Exception as exc:
                # Failed application is stored as a terminal review outcome for follow-up.
                obj.mark_apply_failed(reviewer=request.user, notes=str(exc))
                obj.save(
                    update_fields=[
                        "status",
                        "reviewer",
                        "reviewer_notes",
                        "decided_at",
                        "updated_at",
                    ]
                )
                return error_response(
                    message="Approval failed while applying changes.",
                    error={"error": str(exc)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return success_response(
                message="Platform role change request approved.",
                data=PlatformRoleChangeRequestSerializer(obj, context={"request": request}).data,
            )

        return error_response(
            message="Invalid action. Must be APPROVE or DENY.",
            error={"action": ["Must be APPROVE or DENY."]},
        )


# -----------------------------------------------------------------------------
# Super Admin Transfer
# -----------------------------------------------------------------------------

# Transfer the singleton Vision super-admin role to another Vision staff user.
class TransferSuperAdminView(APIView):
    """
    POST platform/transfer-super-admin/

    Allows the current Vision Super Admin to transfer their role to another
    Vision Staff member. The caller is demoted to Vision Platform Admin.

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
