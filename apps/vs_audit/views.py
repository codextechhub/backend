from __future__ import annotations

from django.db.models import Q
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AuditEvent,
    EntityAuditTrail,
    AuditExportJob,
    ComplianceRule,
)
from .serializers import (
    AuditEventListSerializer,
    AuditEventDetailSerializer,
    EntityAuditTrailSerializer,
    EntityAuditTrailDetailSerializer,
    AuditExportJobListSerializer,
    AuditExportJobDetailSerializer,
    ComplianceRuleListSerializer,
    ComplianceRuleDetailSerializer,
    ComplianceRuleCreateUpdateSerializer,
    AuditEventFilterSerializer,
)


# -----------------------------------------------------------------------------
# Simple placeholder permissions
# Replace these later with your real RBAC permissions
# -----------------------------------------------------------------------------

class IsVisionStaff(permissions.BasePermission):
    """
    Example permission:
    only authenticated staff users can access.
    Replace with your real permission logic later.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)


class IsVisionSuperAdmin(permissions.BasePermission):
    """
    Example super admin permission.
    Replace 'is_superuser' with your own platform super admin logic if needed.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)


# -----------------------------------------------------------------------------
# Audit Event Views
# -----------------------------------------------------------------------------

class AuditEventListView(generics.ListAPIView):
    """
    GET /audit/events/

    Returns paginated audit events.
    Supports filtering with query params.
    """

    serializer_class = AuditEventListSerializer
    permission_classes = [IsVisionStaff]

    def get_queryset(self):
        queryset = AuditEvent.objects.select_related("actor_user").all()

        # Validate incoming filters first
        filter_serializer = AuditEventFilterSerializer(data=self.request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        filters = filter_serializer.validated_data

        i_slug = filters.get("i_slug")
        module_key = filters.get("module_key")
        action_type = filters.get("action_type")
        severity = filters.get("severity")
        status = filters.get("status")
        actor_type = filters.get("actor_type")
        actor_user_id = filters.get("actor_user_id")
        entity_type = filters.get("entity_type")
        entity_id = filters.get("entity_id")
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        search = filters.get("search")

        if i_slug:
            queryset = queryset.filter(i_slug=i_slug)

        if module_key:
            queryset = queryset.filter(module_key=module_key)

        if action_type:
            queryset = queryset.filter(action_type=action_type)

        if severity:
            queryset = queryset.filter(severity=severity)

        if status:
            queryset = queryset.filter(status=status)

        if actor_type:
            queryset = queryset.filter(actor_type=actor_type)

        if actor_user_id:
            queryset = queryset.filter(actor_user_id=actor_user_id)

        if entity_type:
            queryset = queryset.filter(entity_type=entity_type)

        if entity_id:
            queryset = queryset.filter(entity_id=entity_id)

        if date_from:
            queryset = queryset.filter(event_at__gte=date_from)

        if date_to:
            queryset = queryset.filter(event_at__lte=date_to)

        if search:
            queryset = queryset.filter(
                Q(summary__icontains=search) |
                Q(entity_label__icontains=search) |
                Q(entity_id__icontains=search) |
                Q(actor_label__icontains=search)
            )

        return queryset.order_by("-event_at")


class AuditEventDetailView(generics.RetrieveAPIView):
    """
    GET /audit/events/<uuid:id>/

    Returns one audit event in full detail.
    """

    queryset = AuditEvent.objects.select_related(
        "actor_user",
    ).all()
    serializer_class = AuditEventDetailSerializer
    permission_classes = [IsVisionStaff]
    lookup_field = "id"


# -----------------------------------------------------------------------------
# Entity Trail View
# -----------------------------------------------------------------------------

class EntityAuditTrailDetailView(APIView):
    """
    GET /audit/entity-trails/<str:entity_type>/<str:entity_id>/
    """

    permission_classes = [IsVisionStaff]

    def get(self, request, entity_type, entity_id):
        trail_qs = EntityAuditTrail.objects.filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )

        event_qs = AuditEvent.objects.select_related(
            "actor_user",
        ).filter(
            entity_type=entity_type,
            entity_id=entity_id,
        )

        trail = trail_qs.first()
        if not trail:
            return Response(
                {
                    "detail": "No audit trail found for this entity."
                },
                status=404,
            )

        data = {
            "trail": EntityAuditTrailSerializer(trail).data,
            "events": AuditEventListSerializer(
                event_qs.order_by("-event_at", "-created_at"),
                many=True,
            ).data,
        }

        serializer = EntityAuditTrailDetailSerializer(data)
        return Response(serializer.data)


# -----------------------------------------------------------------------------
# Audit Export Job Views
# -----------------------------------------------------------------------------

class AuditExportJobListView(generics.ListAPIView):
    """
    GET /audit/exports/

    Returns export job history.
    """

    serializer_class = AuditExportJobListSerializer
    permission_classes = [IsVisionStaff]

    def get_queryset(self):
        queryset = AuditExportJob.objects.select_related(
            "requested_by",
        ).all()

        status_value = self.request.query_params.get("status")

        if status_value:
            queryset = queryset.filter(status=status_value)

        return queryset.order_by("-requested_at")


class AuditExportJobDetailView(generics.RetrieveAPIView):
    """
    GET /audit/exports/<uuid:id>/
    """

    queryset = AuditExportJob.objects.select_related(
        "requested_by",
    ).all()
    serializer_class = AuditExportJobDetailSerializer
    permission_classes = [IsVisionStaff]
    lookup_field = "id"


# -----------------------------------------------------------------------------
# Compliance Rule Views
# -----------------------------------------------------------------------------

class ComplianceRuleListCreateView(generics.ListCreateAPIView):
    """
    GET /audit/compliance-rules/
    POST /audit/compliance-rules/

    List all rules or create a new one.
    """

    permission_classes = [IsVisionSuperAdmin]

    def get_queryset(self):
        queryset = ComplianceRule.objects.select_related("institution").all()

        i_slug = self.request.query_params.get("i_slug")
        rule_type = self.request.query_params.get("rule_type")
        is_active = self.request.query_params.get("is_active")

        if i_slug:
            queryset = queryset.filter(i_slug=i_slug)

        if rule_type:
            queryset = queryset.filter(rule_type=rule_type)

        if is_active is not None:
            if is_active.lower() == "true":
                queryset = queryset.filter(is_active=True)
            elif is_active.lower() == "false":
                queryset = queryset.filter(is_active=False)

        return queryset.order_by("name")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ComplianceRuleCreateUpdateSerializer
        return ComplianceRuleListSerializer


class ComplianceRuleDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET /audit/compliance-rules/<uuid:id>/
    PUT /audit/compliance-rules/<uuid:id>/
    PATCH /audit/compliance-rules/<uuid:id>/
    DELETE /audit/compliance-rules/<uuid:id>/
    """

    queryset = ComplianceRule.objects.select_related("institution").all()
    permission_classes = [IsVisionSuperAdmin]
    lookup_field = "id"

    def get_serializer_class(self):
        if self.request.method in ["PUT", "PATCH"]:
            return ComplianceRuleCreateUpdateSerializer
        return ComplianceRuleDetailSerializer