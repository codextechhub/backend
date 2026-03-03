from __future__ import annotations

from django.db.models import Q
from rest_framework import generics
from rest_framework.response import Response
# from rest_framework.permissions import AllowAny

from ..models import Institution, InstitutionStatus
from ..permissions import IsVisionStaff, IsVisionSuperAdmin, ExternalOnly
from ..serializers import (
    InstitutionCreateSerializer,
    InstitutionDetailSerializer,
    InstitutionListSerializer,
    InstitutionUpdateSerializer,
)


class ActorContextMixin:
    """Adds actor_id into serializer context (for audit/events)."""

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = str(getattr(user, "id", "system"))
        return ctx


class InstitutionListView(ActorContextMixin, generics.ListAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionListSerializer

    queryset = (
        Institution.objects.all()
        .select_related("branding", "provisioning", "primary_admin")
        .prefetch_related("module_settings")
    )

    def get_queryset(self):
        qs = super().get_queryset()

        # user = getattr(self.request, "user", None)
        # is_super = bool(getattr(user, "is_superuser", False))

        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.ACTIVE)

        pending_param = (self.request.query_params.get("pending") or "").strip().lower()
        if pending_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.PENDING)

        suspended_param = (self.request.query_params.get("suspended") or "").strip().lower()
        if suspended_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.SUSPENDED)
        
        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.INACTIVE)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(institution_name__icontains=q)
                | Q(institution_slug__icontains=q)
                | Q(country__icontains=q)
                | Q(region__icontains=q)
            )

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "institution_name", "-institution_name"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("created_at")
        return qs


class InstitutionCreateView(ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionCreateSerializer


class InstitutionDetailView(ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionDetailSerializer

    queryset = (
        Institution.objects.all()
        .select_related("branding", "provisioning", "primary_admin")
        .prefetch_related("module_settings", "lifecycle_events", "operation_events", "audit_events")
    )
    lookup_field = "id"


class InstitutionUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Separate update endpoint. Returns a full detail payload after update
    so the UI doesn't need to refetch.
    """
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionUpdateSerializer

    queryset = (
        Institution.objects.all()
        .select_related("branding", "provisioning", "primary_admin__contact")
        .prefetch_related("module_settings")
    )
    lookup_field = "id"

    def update(self, request, *args, **kwargs):
        resp = super().update(request, *args, **kwargs)
        institution = self.get_object()
        return Response(InstitutionDetailSerializer(institution, context=self.get_serializer_context()).data)


class InstitutionHardDeleteView(ActorContextMixin, generics.DestroyAPIView):
    """
    Hard delete as a dedicated endpoint. Super admin only.
    NOTE: replace instance.delete() with DeletionService for storage cleanup/reporting.
    """
    permission_classes = [IsVisionSuperAdmin]
    queryset = Institution.objects.all()
    lookup_field = "id"
