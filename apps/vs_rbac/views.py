from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
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
)


# -----------------------------------------------------------------------------
# Permission vocabulary — Module / Resource / Action (Vision-owned)
# -----------------------------------------------------------------------------

class PermissionModuleListCreateView(CreateModelMixin, generics.ListCreateAPIView):
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
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.create"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


class PermissionModuleDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
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


class PermissionResourceListCreateView(CreateModelMixin, generics.ListCreateAPIView):
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


class PermissionResourceDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
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


class PermissionActionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
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


class PermissionActionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
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
class PermissionListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    queryset = Permission.objects.select_related("module", "resource", "action").order_by("module", "action", "key")
    serializer_class = PermissionSerializer
    pagination_class = XVSPagination

    def get_permissions(self):
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
                Q(module_id__icontains=search) |
                Q(resource__name__icontains=search) |
                Q(action_id__icontains=search) |
                Q(description__icontains=search)
            )

        return qs


class PermissionDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = Permission.objects.prefetch_related(
        "groups", "dependencies__depends_on", "required_by__permission"
    ).all()
    lookup_field = "key"

    def get_serializer_class(self):
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


class PermissionDependencyListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    pagination_class = XVSPagination

    def get_permissions(self):
        if self.request.method == "POST":
            self.rbac_permission = "platform.permissions.manage"
        else:
            self.rbac_permission = "platform.permissions.view"
        return [IsAuthenticatedAndActive(), HasRBACPermission()]


class PermissionDependencyDetailView(RetrieveModelMixin, DestroyModelMixin, generics.RetrieveDestroyAPIView):
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
class PermissionGroupListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list all permission groups
    - POST: create a new permission group with optional permission_keys
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
        if self.request.method == "POST":
            return PermissionGroupDetailSerializer
        return PermissionGroupListSerializer


class PermissionGroupDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: group detail with expanded permissions
    - PATCH/PUT: update group fields and optionally replace permission_keys
    - DELETE: blocked for system groups
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
            return error_response(
                message="System permission groups cannot be deleted.",
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().delete(request, *args, **kwargs)


# -----------------------------------------------------------------------------
# School Role Templates
# -----------------------------------------------------------------------------
class SchoolRoleTemplateListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list role templates in a school
    - POST: create a role template in a school
    """
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qp = self.request.query_params

        qs = (
            SchoolRoleTemplate.objects.filter(school_id=school_slug)
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
        serializer.save(school_id=self.kwargs["school_slug"])


class SchoolRoleTemplateDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    School-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: blocked for system or locked roles
    """
    serializer_class = SchoolRoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolRoleTemplate.objects.filter(school_id=school_slug)
            .select_related("created_by", "school")
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
# School User Role Assignments
# -----------------------------------------------------------------------------
class SchoolUserRoleAssignmentListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list assignments in a school
    - POST: assign a role to a user inside a school
    """
    serializer_class = SchoolUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]

        qs = (
            SchoolUserRoleAssignment.objects.filter(school_id=school_slug)
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
        serializer.save(school_id=self.kwargs["school_slug"])


class SchoolUserRoleAssignmentDetailView(RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    School-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow
    """
    serializer_class = SchoolUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolUserRoleAssignment.objects.filter(school_id=school_slug)
            .select_related("user", "role", "assigned_by", "revoked_by", "school")
        )


# -----------------------------------------------------------------------------
# School Role Change Requests (school-internal approval)
# -----------------------------------------------------------------------------
class SchoolRoleChangeRequestListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list requests for a school
    - POST: create a change request for a role in that school
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qs = (
            SchoolRoleChangeRequest.objects.filter(school_id=school_slug)
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
        serializer.save(school_id=self.kwargs["school_slug"])


class SchoolRoleChangeRequestApprovalQueueView(generics.ListAPIView):
    """
    School-admin-facing:
    - GET: pending role change requests for a school
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    pagination_class = XVSPagination

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        qs = (
            SchoolRoleChangeRequest.objects.filter(school_id=school_slug)
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


class SchoolRoleChangeRequestApprovalDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
    """
    School-admin-facing:
    - GET: single role change request within the school
    """
    serializer_class = SchoolRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_slug = self.kwargs["school_slug"]
        return (
            SchoolRoleChangeRequest.objects.filter(school_id=school_slug)
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
        )


class SchoolRoleChangeRequestDecisionView(APIView):
    """
    School-admin decision endpoint for school role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }
    """
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]

    def post(self, request, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = SchoolRoleChangeRequest.objects.select_related("target_role", "school").get(id=request_id)
        except SchoolRoleChangeRequest.DoesNotExist:
            return error_response(
                message="Request not found.",
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != SchoolRoleChangeRequest.Status.PENDING:
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
                    # USE SERVICE LAYER
                    from .services import apply_school_role_change_request
                    apply_school_role_change_request(
                        obj=obj,
                        reviewer=request.user,
                        notes=notes
                    )

            except Exception as exc:
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
class PlatformRoleTemplateListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform roles
    - POST: create platform role
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

        if status_q:
            qs = qs.filter(status=status_q)

        if is_locked is not None:
            lowered = is_locked.lower()
            if lowered in {"true", "1"}:
                qs = qs.filter(is_locked=True)
            elif lowered in {"false", "0"}:
                qs = qs.filter(is_locked=False)
        if search := self.request.query_params.get("search"):
            qs = qs.filter(Q(name__icontains=search))
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PlatformRoleTemplateDetailSerializer
        return PlatformRoleTemplateListSerializer


class PlatformRoleTemplateDetailView(RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: detail of a platform role
    - PATCH/PUT: blocked for locked or system roles
    - DELETE: blocked for system or locked roles
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
class PlatformUserRoleAssignmentListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform user role assignments
    - POST: assign platform role to internal user
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

        if user_id:
            qs = qs.filter(user_id=user_id)
        if role_id:
            qs = qs.filter(role_id=role_id)
        if assignment_status:
            qs = qs.filter(assignment_status=assignment_status)

        return qs

    def perform_create(self, serializer):
        serializer.save()


class PlatformUserRoleAssignmentDetailView(RetrieveModelMixin, UpdateModelMixin, generics.RetrieveUpdateAPIView):
    """
    Vision-facing:
    - GET: one platform assignment
    - PATCH: often used to revoke
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


# -----------------------------------------------------------------------------
# Platform Role Change Requests
# -----------------------------------------------------------------------------
class PlatformRoleChangeRequestListCreateView(CreateModelMixin, generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform role change requests
    - POST: create platform role change request
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


class PlatformRoleChangeRequestDetailView(RetrieveModelMixin, generics.RetrieveAPIView):
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


class PlatformRoleChangeRequestDecisionView(APIView):
    """
    Vision-facing decision endpoint for platform role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }
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
                    from .services import apply_platform_role_change_request
                    apply_platform_role_change_request(
                        obj=obj,
                        reviewer=request.user,
                        notes=notes,
                    )

            except Exception as exc:
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

class TransferSuperAdminView(APIView):
    """
    POST platform/transfer-super-admin/

    Allows the current Vision Super Admin to transfer their role to another
    Vision Staff member. The caller is demoted to Vision Platform Admin.

    Body: { "new_super_admin_id": "<uuid>" }
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
            transfer_super_admin(from_user=request.user, to_user=new_user)
        except ValueError as exc:
            return error_response(message=str(exc), error={})

        return success_response(
            message=f"Super admin role transferred to {new_user.email}. You are now a Platform Admin.",
        )