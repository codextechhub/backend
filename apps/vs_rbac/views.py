from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Permission,
    PermissionDependency,
    RoleTemplate,
    UserRoleAssignment,
    RoleChangeRequest,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    PlatformRoleChangeRequest,
)
from .serializers import (
    PermissionSerializer,
    PermissionDependencySerializer,
    RoleTemplateListSerializer,
    RoleTemplateDetailSerializer,
    UserRoleAssignmentSerializer,
    RoleChangeRequestSerializer,
    PlatformRoleTemplateListSerializer,
    PlatformRoleTemplateDetailSerializer,
    PlatformUserRoleAssignmentSerializer,
    PlatformRoleChangeRequestSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsInstitutionAdmin,
)
# Optional:
# from .services import apply_branch_role_change_request, apply_platform_role_change_request


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
# Branch Role Templates
# -----------------------------------------------------------------------------
class RoleTemplateListCreateView(generics.ListCreateAPIView):
    """
    Branch-facing:
    - GET: list role templates in a branch
    - POST: create a role template in a branch
    """
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        branch_id = self.kwargs["branch_id"]
        return (
            RoleTemplate.objects.filter(branch_id=branch_id)
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
            .select_related("created_by", "branch")
            .order_by("name")
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return RoleTemplateDetailSerializer
        return RoleTemplateListSerializer

    def perform_create(self, serializer):
        serializer.save(branch_id=self.kwargs["branch_id"])


class RoleTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Branch-facing:
    - GET: role detail
    - PATCH/PUT: update role fields and optionally replace permission_keys
    - DELETE: hard delete (if your policy allows it)
    """
    serializer_class = RoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]
    lookup_field = "id"

    def get_queryset(self):
        branch_id = self.kwargs["branch_id"]
        return (
            RoleTemplate.objects.filter(branch_id=branch_id)
            .select_related("created_by", "branch")
            .prefetch_related("role_permissions__permission")
        )


# -----------------------------------------------------------------------------
# Branch User Role Assignments
# -----------------------------------------------------------------------------
class UserRoleAssignmentListCreateView(generics.ListCreateAPIView):
    """
    Branch-facing:
    - GET: list assignments in a branch
    - POST: assign a role to a user inside a branch
    """
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        branch_id = self.kwargs["branch_id"]

        qs = (
            UserRoleAssignment.objects.filter(branch_id=branch_id)
            .select_related("user", "role", "assigned_by", "revoked_by", "branch")
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
        serializer.save(branch_id=self.kwargs["branch_id"])


class UserRoleAssignmentDetailView(generics.RetrieveUpdateAPIView):
    """
    Branch-facing:
    - GET: one assignment
    - PATCH: often used for revoke flow
    """
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]
    lookup_field = "id"

    def get_queryset(self):
        branch_id = self.kwargs["branch_id"]
        return (
            UserRoleAssignment.objects.filter(branch_id=branch_id)
            .select_related("user", "role", "assigned_by", "revoked_by", "branch")
        )


# -----------------------------------------------------------------------------
# Branch Role Change Requests (Branch -> Vision)
# -----------------------------------------------------------------------------
class BranchRoleChangeRequestListCreateView(generics.ListCreateAPIView):
    """
    Branch-facing:
    - GET: list requests for a branch
    - POST: create a change request for a role in that branch
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        branch_id = self.kwargs["branch_id"]
        qs = (
            RoleChangeRequest.objects.filter(branch_id=branch_id)
            .select_related("requested_by", "reviewer", "target_role", "branch")
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
        serializer.save(branch_id=self.kwargs["branch_id"])


class VisionRoleChangeRequestQueueView(generics.ListAPIView):
    """
    Vision-facing:
    - GET: queue of branch role change requests across branches
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def get_queryset(self):
        qs = (
            RoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role", "branch")
            .prefetch_related("delta_items__permission")
            .order_by("-submitted_at")
        )

        status_q = self.request.query_params.get("status")
        branch_id = self.request.query_params.get("branch_id")
        target_role = self.request.query_params.get("target_role")

        if status_q:
            qs = qs.filter(status=status_q)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if target_role:
            qs = qs.filter(target_role_id=target_role)

        return qs


class VisionRoleChangeRequestDetailView(generics.RetrieveAPIView):
    """
    Vision-facing:
    - GET: single branch role change request
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    lookup_field = "id"

    def get_queryset(self):
        return (
            RoleChangeRequest.objects.all()
            .select_related("requested_by", "reviewer", "target_role", "branch")
            .prefetch_related("delta_items__permission")
        )


class VisionRoleChangeRequestDecisionView(APIView):
    """
    Vision-facing decision endpoint for branch role change requests.

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
            obj = RoleChangeRequest.objects.select_related("target_role", "branch").get(id=request_id)
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
                    # Recommended:
                    # apply_branch_role_change_request(obj=obj, reviewer=request.user, notes=notes)

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
            .prefetch_related("role_permissions__permission")
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