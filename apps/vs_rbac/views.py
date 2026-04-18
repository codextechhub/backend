from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Permission,
    PermissionDependency,
    PermissionGroup,
    PlatformRoleChangeRequest,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    RoleChangeRequest,
    RoleTemplate,
    UserRoleAssignment,
)
from .serializers import (
    PermissionDependencySerializer,
    PermissionGroupDetailSerializer,
    PermissionGroupListSerializer,
    PermissionSerializer,
    PlatformRoleChangeRequestSerializer,
    PlatformRoleTemplateDetailSerializer,
    PlatformRoleTemplateListSerializer,
    PlatformUserRoleAssignmentSerializer,
    RoleChangeRequestSerializer,
    RoleTemplateDetailSerializer,
    RoleTemplateListSerializer,
    UserRoleAssignmentSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsSchoolAdmin,
)


# -----------------------------------------------------------------------------
# Global Permission Registry (Vision-owned)
# -----------------------------------------------------------------------------
class PermissionListCreateView(generics.ListCreateAPIView):
    queryset = Permission.objects.all().order_by("module_key", "action", "key")
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]


class PermissionDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Permission.objects.all()
    serializer_class = PermissionSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "key"


class PermissionDependencyListCreateView(generics.ListCreateAPIView):
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]


class PermissionDependencyDetailView(generics.RetrieveDestroyAPIView):
    queryset = PermissionDependency.objects.select_related("permission", "depends_on").all()
    serializer_class = PermissionDependencySerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"


# -----------------------------------------------------------------------------
# Permission Groups (Vision-owned, shared across school + platform roles)
# -----------------------------------------------------------------------------
class PermissionGroupListCreateView(generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list all permission groups
    - POST: create a new permission group with optional permission_keys
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

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

        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PermissionGroupDetailSerializer
        return PermissionGroupListSerializer


class PermissionGroupDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: group detail with expanded permissions
    - PATCH/PUT: update group fields and optionally replace permission_keys
    - DELETE: hard delete (system groups should be protected by UI layer)
    """
    serializer_class = PermissionGroupDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"

    def get_queryset(self):
        return PermissionGroup.objects.all().prefetch_related("permissions")


# -----------------------------------------------------------------------------
# School Role Templates
# -----------------------------------------------------------------------------
class RoleTemplateListCreateView(generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list role templates in a school
    - POST: create a role template in a school
    """
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]

    def get_queryset(self):
        school_id = self.kwargs["school_id"]
        return (
            RoleTemplate.objects.filter(school_id=school_id)
            .annotate(
                assigned_users_count=Count(
                    "user_assignments",
                    filter=Q(user_assignments__assignment_status=UserRoleAssignment.AssignmentStatus.ACTIVE),
                    distinct=True,
                ),
                permissions_count=Count(
                    "role_permissions",
                    filter=Q(role_permissions__granted=True),
                    distinct=True,
                ),
            )
            .select_related("created_by", "school")
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return RoleTemplateDetailSerializer
        return RoleTemplateListSerializer

    def perform_create(self, serializer):
        serializer.save(school_id=self.kwargs["school_id"])


class RoleTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    School-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: hard delete (if your policy allows it)
    """
    serializer_class = RoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_id = self.kwargs["school_id"]
        return (
            RoleTemplate.objects.filter(school_id=school_id)
            .select_related("created_by", "school")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )


# -----------------------------------------------------------------------------
# School User Role Assignments
# -----------------------------------------------------------------------------
class UserRoleAssignmentListCreateView(generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list assignments in a school
    - POST: assign a role to a user inside a school
    """
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]

    def get_queryset(self):
        school_id = self.kwargs["school_id"]

        qs = (
            UserRoleAssignment.objects.filter(school_id=school_id)
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
        serializer.save(school_id=self.kwargs["school_id"])


class UserRoleAssignmentDetailView(generics.RetrieveUpdateAPIView):
    """
    School-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow
    """
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]
    lookup_field = "id"

    def get_queryset(self):
        school_id = self.kwargs["school_id"]
        return (
            UserRoleAssignment.objects.filter(school_id=school_id)
            .select_related("user", "role", "assigned_by", "revoked_by", "school")
        )


# -----------------------------------------------------------------------------
# School Role Change Requests (School -> Vision)
# -----------------------------------------------------------------------------
class SchoolRoleChangeRequestListCreateView(generics.ListCreateAPIView):
    """
    School-facing:
    - GET: list requests for a school
    - POST: create a change request for a role in that school
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsSchoolAdmin]

    def get_queryset(self):
        school_id = self.kwargs["school_id"]
        qs = (
            RoleChangeRequest.objects.filter(school_id=school_id)
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
        serializer.save(school_id=self.kwargs["school_id"])


class VisionRoleChangeRequestQueueView(generics.ListAPIView):
    """
    Vision-facing:
    - GET: queue of school role change requests across schools
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def get_queryset(self):
        qs = (
            RoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )

        status_q = self.request.query_params.get("status")
        school_id = self.request.query_params.get("school_id")
        target_role = self.request.query_params.get("target_role")

        if status_q:
            qs = qs.filter(status=status_q)
        if school_id:
            qs = qs.filter(school_id=school_id)
        if target_role:
            qs = qs.filter(target_role_id=target_role)

        return qs


class VisionRoleChangeRequestDetailView(generics.RetrieveAPIView):
    """
    Vision-facing:
    - GET: single school role change request
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"

    def get_queryset(self):
        return (
            RoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role", "school")
            .prefetch_related("delta_items__permission")
        )


class VisionRoleChangeRequestDecisionView(APIView):
    """
    Vision-facing decision endpoint for school role change requests.

    POST body:
    {
        "action": "APPROVE" | "DENY",
        "notes": "optional approval notes / required denial reason"
    }
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def post(self, request, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = RoleChangeRequest.objects.select_related("target_role", "school").get(id=request_id)
        except RoleChangeRequest.DoesNotExist:
            return Response(
                {"detail": "Request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != RoleChangeRequest.Status.PENDING:
            return Response(
                {"detail": f"Request already decided ({obj.status})."},
                status=status.HTTP_409_CONFLICT,
            )

        if action == "DENY":
            if not notes:
                return Response(
                    {"notes": ["Denial reason is required."]},
                    status=status.HTTP_400_BAD_REQUEST,
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
            return Response(
                RoleChangeRequestSerializer(obj, context={"request": request}).data,
                status=status.HTTP_200_OK,
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
                return Response(
                    {
                        "detail": "Approval failed while applying changes.",
                        "error": str(exc),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response(
                RoleChangeRequestSerializer(obj, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        return Response(
            {"action": ["Must be APPROVE or DENY."]},
            status=status.HTTP_400_BAD_REQUEST,
        )


# -----------------------------------------------------------------------------
# Platform Role Templates (Vision/internal)
# -----------------------------------------------------------------------------
class PlatformRoleTemplateListCreateView(generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform roles
    - POST: create platform role
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

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

        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PlatformRoleTemplateDetailSerializer
        return PlatformRoleTemplateListSerializer


class PlatformRoleTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Vision-facing:
    - GET: detail of a platform role
    - PATCH/PUT: update role and permission_keys
    """
    serializer_class = PlatformRoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"

    def get_queryset(self):
        return (
            PlatformRoleTemplate.objects.all()
            .select_related("created_by")
            .prefetch_related(
                "role_permissions__permission",
                "role_groups__group",
            )
        )


# -----------------------------------------------------------------------------
# Platform User Role Assignments
# -----------------------------------------------------------------------------
class PlatformUserRoleAssignmentListCreateView(generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform user role assignments
    - POST: assign platform role to internal user
    """
    serializer_class = PlatformUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

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


class PlatformUserRoleAssignmentDetailView(generics.RetrieveUpdateAPIView):
    """
    Vision-facing:
    - GET: one platform assignment
    - PATCH: often used to revoke
    """
    serializer_class = PlatformUserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"

    def get_queryset(self):
        return (
            PlatformUserRoleAssignment.objects.all()
            .select_related("user", "role", "assigned_by", "revoked_by")
        )


# -----------------------------------------------------------------------------
# Platform Role Change Requests
# -----------------------------------------------------------------------------
class PlatformRoleChangeRequestListCreateView(generics.ListCreateAPIView):
    """
    Vision-facing:
    - GET: list platform role change requests
    - POST: create platform role change request
    """
    serializer_class = PlatformRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

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


class PlatformRoleChangeRequestDetailView(generics.RetrieveAPIView):
    serializer_class = PlatformRoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
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
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def post(self, request, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = PlatformRoleChangeRequest.objects.select_related("target_role").get(id=request_id)
        except PlatformRoleChangeRequest.DoesNotExist:
            return Response(
                {"detail": "Request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if obj.status != PlatformRoleChangeRequest.Status.PENDING:
            return Response(
                {"detail": f"Request already decided ({obj.status})."},
                status=status.HTTP_409_CONFLICT,
            )

        if action == "DENY":
            if not notes:
                return Response(
                    {"notes": ["Denial reason is required."]},
                    status=status.HTTP_400_BAD_REQUEST,
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
            return Response(
                PlatformRoleChangeRequestSerializer(obj, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        if action == "APPROVE":
            try:
                with transaction.atomic():
                    # Recommended:
                    # apply_platform_role_change_request(obj=obj, reviewer=request.user, notes=notes)

                    # Placeholder version:
                    obj.mark_approved(reviewer=request.user, notes=notes)
                    obj.save(
                        update_fields=[
                            "status",
                            "reviewer",
                            "reviewer_notes",
                            "decided_at",
                            "updated_at",
                        ]
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
                return Response(
                    {
                        "detail": "Approval failed while applying changes.",
                        "error": str(exc),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response(
                PlatformRoleChangeRequestSerializer(obj, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        return Response(
            {"action": ["Must be APPROVE or DENY."]},
            status=status.HTTP_400_BAD_REQUEST,
        )