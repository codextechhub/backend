from __future__ import annotations

from django.db.models import Q
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from ..models import School, SchoolStatus
from ..permissions import IsVisionStaff, IsVisionSuperAdmin
from ..serializers import (
    SchoolCreateSerializer,
    SchoolDetailSerializer,
    SchoolListSerializer,
    SchoolUpdateSerializer,
)
from ..paginations import SchoolPagination


class ActorContextMixin:
    """Adds actor_id into serializer context (for audit/events)."""

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class SchoolListView(ActorContextMixin, generics.ListAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = SchoolListSerializer
    pagination_class = SchoolPagination

    queryset = (
        School.objects.all()
        .select_related("branding",)
        .prefetch_related("branches")
    )

    def get_queryset(self):
        qs = super().get_queryset()

        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            qs = qs.filter(status=SchoolStatus.ACTIVE)

        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            qs = qs.filter(status=SchoolStatus.INACTIVE)

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


class SchoolStatsView(generics.GenericAPIView):
    """
    Returns a single summary payload with school counts broken down
    by status. Designed for the School Management dashboard stat cards.

    Response shape:
        {
            "all":      47,
            "active":   32,
            "pending":  8,
            "inactive": 7
        }

    One DB query using conditional aggregation — no N+1.
    """
    permission_classes = [IsVisionStaff]

    def get(self, request, *args, **kwargs):
        from django.db.models import Count, Q

        result = School.objects.aggregate(
            all=Count("slug"),
            active=Count("slug", filter=Q(status=SchoolStatus.ACTIVE)),
            pending=Count("slug", filter=Q(status=SchoolStatus.PENDING)),
            inactive=Count("slug", filter=Q(status=SchoolStatus.INACTIVE)),
        )

        return Response(result)
    

class SchoolCreateView(ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = SchoolCreateSerializer


class SchoolDetailView(ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = SchoolDetailSerializer

    queryset = (
        School.objects.all()
        .select_related("branding")
        .prefetch_related("branches")
    )
    lookup_field = "slug"


class SchoolUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Separate update endpoint. Returns a full detail payload after update
    so the UI doesn't need to refetch.
    """
    permission_classes = [IsVisionStaff]
    serializer_class = SchoolUpdateSerializer

    queryset = (
        School.objects.all()
        .select_related("branding")
        .prefetch_related("branches")
    )
    lookup_field = "slug"

    def update(self, request, *args, **kwargs):
        resp = super().update(request, *args, **kwargs)
        school = self.get_object()
        return Response(
            SchoolDetailSerializer(school, context=self.get_serializer_context()).data
        )