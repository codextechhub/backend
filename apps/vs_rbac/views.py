from __future__ import annotations

from django.db.models import Count
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Permission,
    PermissionDependency,
    RoleTemplate,
    RoleVersionSnapshot,
    UserRoleAssignment,
    RoleChangeRequest,
    RoleLockEvent,
    EffectivePermissionCache,
)
from .serializers import (
    PermissionSerializer,
    PermissionDependencySerializer,
    RoleTemplateListSerializer,
    RoleTemplateDetailSerializer,
    UserRoleAssignmentSerializer,
    RoleVersionSnapshotSerializer,
    RoleChangeRequestSerializer,
    RoleLockEventSerializer,
    EffectivePermissionCacheSerializer,
)
from .permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsInstitutionAdmin,
    ReadOnly,
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
# Role Templates (Institution-scoped)
# -----------------------------------------------------------------------------
class RoleTemplateListCreateView(generics.ListCreateAPIView):
    """
    GET: list roles in an institution
    POST: create role in an institution
    """
    serializer_class = RoleTemplateListSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return (
            RoleTemplate.objects.filter(institution_id=institution_id)
            .annotate(assigned_users_count=Count("user_assignments", distinct=True))
            .order_by("name")
        )

    def get_serializer_class(self):
        # Use the detailed serializer for create so it accepts permission_keys
        if self.request.method == "POST":
            return RoleTemplateDetailSerializer
        return RoleTemplateListSerializer

    def perform_create(self, serializer):
        # Force institution from URL (prevents cross-institution writes)
        serializer.save(institution_id=self.kwargs["institution_id"])


class RoleTemplateDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET: role details (includes role_permissions)
    PATCH/PUT: update role fields (+ optional permission_keys to replace)
    DELETE: optional — you can disable hard delete in policy and use archive instead
    """
    serializer_class = RoleTemplateDetailSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]
    lookup_field = "id"

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return RoleTemplate.objects.filter(institution_id=institution_id).prefetch_related(
            "role_permissions__permission"
        )


# -----------------------------------------------------------------------------
# Role Version Snapshots (usually read-only)
# -----------------------------------------------------------------------------
class RoleSnapshotListView(generics.ListAPIView):
    serializer_class = RoleVersionSnapshotSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        role_id = self.kwargs["role_id"]
        return RoleVersionSnapshot.objects.filter(
            role_id=role_id,
            role__institution_id=institution_id,
        ).order_by("-version_number")


class RoleSnapshotDetailView(generics.RetrieveAPIView):
    serializer_class = RoleVersionSnapshotSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]
    lookup_field = "id"

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return RoleVersionSnapshot.objects.filter(role__institution_id=institution_id)


# -----------------------------------------------------------------------------
# Assign roles to users (Institution-scoped)
# -----------------------------------------------------------------------------
class UserRoleAssignmentListCreateView(generics.ListCreateAPIView):
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        # Optional filters: ?user=<uuid> or ?role=<uuid>
        qs = UserRoleAssignment.objects.filter(institution_id=institution_id).select_related("user", "role")
        user_id = self.request.query_params.get("user")
        role_id = self.request.query_params.get("role")
        if user_id:
            qs = qs.filter(user_id=user_id)
        if role_id:
            qs = qs.filter(role_id=role_id)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(institution_id=self.kwargs["institution_id"])


class UserRoleAssignmentDetailView(generics.RetrieveUpdateAPIView):
    """
    PATCH: typically used to revoke (set assignment_status=REVOKED + reason_note)
    """
    serializer_class = UserRoleAssignmentSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]
    lookup_field = "id"

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return UserRoleAssignment.objects.filter(institution_id=institution_id).select_related("user", "role")


# -----------------------------------------------------------------------------
# Role Change Requests (Institution -> Vision)
# -----------------------------------------------------------------------------
class InstitutionRoleChangeRequestListCreateView(generics.ListCreateAPIView):
    """
    Institution-facing:
    - GET: list requests for an institution
    - POST: create request (requested_by auto)
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsInstitutionAdmin]

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return RoleChangeRequest.objects.filter(institution_id=institution_id).order_by("-submitted_at")

    def perform_create(self, serializer):
        serializer.save(institution_id=self.kwargs["institution_id"])


class VisionRoleChangeRequestQueueView(generics.ListAPIView):
    """
    Vision-facing:
    - GET: queue across institutions
    """
    serializer_class = RoleChangeRequestSerializer
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def get_queryset(self):
        qs = RoleChangeRequest.objects.all().order_by("-submitted_at")
        status_q = self.request.query_params.get("status")
        if status_q:
            qs = qs.filter(status=status_q)
        inst = self.request.query_params.get("institution_id")
        if inst:
            qs = qs.filter(institution_id=inst)
        return qs


class VisionRoleChangeRequestDecisionView(APIView):
    """
    Vision-facing:
    POST /vision/role-change-requests/<id>/decide/
    Body:
      { "action": "APPROVE" | "DENY", "notes": "..." }

    NOTE: Applying the permission changes for APPROVE should be done in a service
    layer (transaction + dependency/compliance checks + snapshots + cache invalidation).
    Here we show the endpoint shape clearly and simply.
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def post(self, request, request_id: str):
        action = (request.data.get("action") or "").upper().strip()
        notes = (request.data.get("notes") or "").strip()

        try:
            obj = RoleChangeRequest.objects.select_related("target_role").get(id=request_id)
        except RoleChangeRequest.DoesNotExist:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

        if obj.status not in {"PENDING"}:
            return Response({"detail": f"Request already decided ({obj.status})."}, status=status.HTTP_409_CONFLICT)

        if action == "DENY":
            if not notes:
                return Response({"notes": ["Denial reason is required."]}, status=status.HTTP_400_BAD_REQUEST)
            obj.mark_denied(reviewer=request.user, notes=notes)
            obj.save(update_fields=["status", "reviewer", "reviewer_notes", "decided_at", "updated_at"])
            return Response(RoleChangeRequestSerializer(obj, context={"request": request}).data)

        if action == "APPROVE":
            # Place-holder: actual application should live in a service function.
            # For now we only mark approved; you can replace this with atomic apply logic.
            obj.mark_approved(reviewer=request.user, notes=notes)
            obj.save(update_fields=["status", "reviewer", "reviewer_notes", "decided_at", "updated_at"])
            return Response(RoleChangeRequestSerializer(obj, context={"request": request}).data)

        return Response({"action": ["Must be APPROVE or DENY."]}, status=status.HTTP_400_BAD_REQUEST)


# -----------------------------------------------------------------------------
# Role lock history (read-only list; lock/unlock action usually is a separate endpoint)
# -----------------------------------------------------------------------------
class RoleLockEventListView(generics.ListAPIView):
    serializer_class = RoleLockEventSerializer
    permission_classes = [IsAuthenticatedAndActive & (IsInstitutionAdmin | IsVisionStaff)]

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return RoleLockEvent.objects.filter(role__institution_id=institution_id).order_by("-created_at")


# -----------------------------------------------------------------------------
# Effective Permission Cache (usually read-only)
# -----------------------------------------------------------------------------
class EffectivePermissionCacheDetailView(generics.RetrieveAPIView):
    serializer_class = EffectivePermissionCacheSerializer
    permission_classes = [IsAuthenticatedAndActive & (IsInstitutionAdmin | IsVisionStaff)]
    lookup_field = "id"

    def get_queryset(self):
        institution_id = self.kwargs["institution_id"]
        return EffectivePermissionCache.objects.filter(institution_id=institution_id)