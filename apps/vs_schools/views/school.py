from __future__ import annotations

from django.db.models import Q
from django.db.models import Prefetch
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from core.mixins import RetrieveModelMixin, CreateModelMixin
from core.pagination import XVSPagination
from core.response import success_response, error_response
from ..models import Branch, School, SchoolStatus
from vs_rbac.permissions import IsAuthenticatedAndActive, HasRBACPermission
from ..serializers import (
    SchoolCreateSerializer,
    SchoolDetailSerializer,
    SchoolListSerializer,
    SchoolUpdateSerializer,
)


class ActorContextMixin:
    """Adds actor_id into serializer context (for audit/events)."""

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = user
        return ctx


class SchoolListView(ActorContextMixin, generics.ListAPIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"
    serializer_class = SchoolListSerializer
    pagination_class = XVSPagination

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
                | Q(ownership_type__iexact=q)
                | Q(status__iexact=q)
                | Q(branches__state__icontains=q)
                | Q(branches__country__icontains=q)
                | Q(branches__name__icontains=q)
            ).distinct()

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name", "status", "-status"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("-created_at")
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
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"

    def get(self, request, *args, **kwargs):
        from django.db.models import Count, Q

        result = School.objects.aggregate(
            all=Count("slug"),
            active=Count("slug", filter=Q(status=SchoolStatus.ACTIVE)),
            pending=Count("slug", filter=Q(status=SchoolStatus.PENDING)),
            inactive=Count("slug", filter=Q(status=SchoolStatus.INACTIVE)),
        )

        return success_response(message="School statistics retrieved.", data=result)
    

class SchoolCreateView(CreateModelMixin, ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.create"
    serializer_class = SchoolCreateSerializer


class SchoolDetailView(ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"
    serializer_class = SchoolDetailSerializer

    queryset = (
        School.objects.all()
        .select_related(
            "branding",
            "primary_admin",
            "primary_admin__contact",
            "package_setup",
            "package_setup__package_plan",
        )
        .prefetch_related(
            Prefetch(
                "branches",
                queryset=Branch.objects.select_related(
                    "primary_admin", "primary_admin__contact"
                ),
            )
        )
    )
    lookup_field = "slug"

    def retrieve(self, request, *args, **kwargs):
        import traceback as tb
        try:
            instance = self.get_object()
            serializer = self.get_serializer(instance)
            return success_response(
                message="Data retrieved successfully.",
                data=serializer.data,
            )
        except Exception as exc:
            return error_response(
                message=f"DEBUG: {type(exc).__name__}: {exc}",
                data={"trace": tb.format_exc()},
            )


class SchoolUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Separate update endpoint. Returns a full detail payload after update
    so the UI doesn't need to refetch.
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.update"
    serializer_class = SchoolUpdateSerializer

    queryset = (
        School.objects.all()
        .select_related(
            "branding",
            "primary_admin",
            "primary_admin__contact",
            "package_setup",
            "package_setup__package_plan",
        )
        .prefetch_related(
            Prefetch(
                "branches",
                queryset=Branch.objects.select_related(
                    "primary_admin", "primary_admin__contact"
                ),
            )
        )
    )
    lookup_field = "slug"

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)
        school = self.get_object()
        return success_response(
            message="School updated successfully.",
            data=SchoolDetailSerializer(school, context=self.get_serializer_context()).data,
        )