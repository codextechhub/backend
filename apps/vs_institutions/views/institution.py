from __future__ import annotations

from django.db.models import Q
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from ..models import Institution, InstitutionStatus
from ..permissions import IsVisionStaff
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
        .select_related("branding",)
        .prefetch_related("module_settings", "branches")
    )

    def get_queryset(self):
        qs = super().get_queryset()

        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.ACTIVE)

        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            qs = qs.filter(status=InstitutionStatus.INACTIVE)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(_type__iexact=q)
                | Q(status__iexact=q)
                | Q(branches__state__icontains=q)
                | Q(branches__city__icontains=q)
                | Q(branches__country__icontains=q)
                | Q(branches__name__icontains=q)
            ).distinct()

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("created_at")
        return qs


class InstitutionCountView(generics.GenericAPIView):
    permission_classes = [IsVisionStaff]

    def get(self, request, *args, **kwargs):
        param = "all"
        count = Institution.objects.count()

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            param = "active"
            count = Institution.objects.filter(status=InstitutionStatus.ACTIVE).count()

        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            param = "inactive"
            count = Institution.objects.filter(status=InstitutionStatus.INACTIVE).count()

        return Response({f"{param.capitalize()} count": count})


class InstitutionCreateView(ActorContextMixin, generics.CreateAPIView):
    permission_classes = [AllowAny]  # Allow any authenticated user to create an institution (or change to IsVisionStaff if you want to restrict)
    serializer_class = InstitutionCreateSerializer


class InstitutionDetailView(ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionDetailSerializer

    queryset = (
        Institution.objects.all()
        .select_related("branding")
        .prefetch_related("branches")
    )
    lookup_field = "slug"


class InstitutionUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Separate update endpoint. Returns a full detail payload after update
    so the UI doesn't need to refetch.
    """
    permission_classes = [IsVisionStaff]
    serializer_class = InstitutionUpdateSerializer

    queryset = (
        Institution.objects.all()
        .select_related("branding")
        .prefetch_related("module_settings", "branches")
    )
    lookup_field = "slug"

    def update(self, request, *args, **kwargs):
        resp = super().update(request, *args, **kwargs)
        institution = self.get_object()
        return Response(
            InstitutionDetailSerializer(institution, context=self.get_serializer_context()).data
        )