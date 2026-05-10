from __future__ import annotations

from django.db.models import Q
from django.forms import ValidationError
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated, AllowAny

from core.mixins import RetrieveModelMixin, CreateModelMixin
from core.response import success_response, error_response

from ..models import Branch, BranchStatus, School
from vs_rbac.permissions import IsVisionStaff, IsAuthenticatedAndActive
from ..serializers import (
    BranchCreateSerializer,
    BranchDetailSerializer,
    BranchListSerializer,
    BranchUpdateSerializer,
)


class ActorContextMixin:
    """Adds actor_id into serializer context (for audit/events)."""

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["actor_id"] = getattr(self.request, "user", None)
        return ctx


class BranchListView(ActorContextMixin, generics.ListAPIView):
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = BranchListSerializer
    queryset = Branch.objects.all().select_related("school")

    def get_queryset(self):
        qs = super().get_queryset()

        # Filter by school (tenant)
        qs = qs.filter(school__slug=self.kwargs.get("slug"))

        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            qs = qs.filter(status__in=statuses)

        active_param = (self.request.query_params.get("active") or "").strip().lower()
        if active_param in ("1", "true", "yes"):
            qs = qs.filter(status=BranchStatus.ACTIVE)

        pending_param = (self.request.query_params.get("pending") or "").strip().lower()
        if pending_param in ("1", "true", "yes"):
            qs = qs.filter(status=BranchStatus.PENDING)

        suspended_param = (self.request.query_params.get("suspended") or "").strip().lower()
        if suspended_param in ("1", "true", "yes"):
            qs = qs.filter(status=BranchStatus.SUSPENDED)

        inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
        if inactive_param in ("1", "true", "yes"):
            qs = qs.filter(status=BranchStatus.INACTIVE)

        closed_param = (self.request.query_params.get("closed") or "").strip().lower()
        if closed_param in ("1", "true", "yes"):
            qs = qs.filter(status=BranchStatus.CLOSED)

        main_param = (self.request.query_params.get("main") or "").strip().lower()
        if main_param in ("1", "true", "yes"):
            qs = qs.filter(is_main=True)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(address__icontains=q)
                | Q(state__icontains=q)
                | Q(country__icontains=q)
                | Q(email__icontains=q)
                | Q(school__name__icontains=q)
                | Q(school__slug__icontains=q)
            )

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name", "code", "-code"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("-created_at")
        return qs


class BranchStatsView(generics.GenericAPIView):
    """
    Returns a single summary payload with branch counts broken down by status.
    Designed for the Branch Management dashboard stat cards.

    Supports optional school scoping via ?s=<school_slug>

    Response shape:
        {
            "all":       94,
            "active":    61,
            "pending":   12,
            "suspended": 8,
            "inactive":  9,
            "closed":    4
        }

    One DB query using conditional aggregation — no N+1.
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]

    def get(self, request, *args, **kwargs):
        from django.db.models import Count, Q

        qs = Branch.objects.all()

        i_slug = self.kwargs.get("slug")
        qs = qs.filter(school__slug=i_slug)

        result = qs.aggregate(
            all=Count("id"),
            active=Count("id", filter=Q(status=BranchStatus.ACTIVE)),
            pending=Count("id", filter=Q(status=BranchStatus.PENDING)),
            suspended=Count("id", filter=Q(status=BranchStatus.SUSPENDED)),
            inactive=Count("id", filter=Q(status=BranchStatus.INACTIVE)),
            closed=Count("id", filter=Q(status=BranchStatus.CLOSED)),
        )

        return success_response(message="Branch statistics retrieved.", data=result)
    

class BranchCreateView(CreateModelMixin, ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = BranchCreateSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        i_slug = self.kwargs["i_slug"]

        school = School.objects.filter(slug=i_slug).first()
        if not school:
            raise NotFound(f"School with slug '{i_slug}' does not exist.")
        if school.status != "ACTIVE":
            raise ValidationError({"detail": "Branches can only be created for active schools."})

        ctx["school"] = school
        return ctx


class BranchDetailView(RetrieveModelMixin, ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = BranchDetailSerializer

    queryset = Branch.objects.all().select_related(
        "school", "primary_admin", "primary_admin__contact"
    )
    lookup_field = "code"

    def get_queryset(self):
        qs = super().get_queryset()
        slug = self.kwargs.get("slug")
        if slug:
            qs = qs.filter(school__slug=slug)
        return qs


class BranchUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Returns a full detail payload after update so the UI doesn't need to refetch.
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = BranchUpdateSerializer

    queryset = Branch.objects.all().select_related(
        "school", "primary_admin", "primary_admin__contact"
    )
    lookup_field = "code"

    def get_queryset(self):
        qs = super().get_queryset()
        slug = self.kwargs.get("slug")
        if slug:
            qs = qs.filter(school__slug=slug)
        return qs

    def update(self, request, *args, **kwargs):
        super().update(request, *args, **kwargs)
        branch = self.get_object()
        return success_response(
            message="Branch updated successfully.",
            data=BranchDetailSerializer(branch, context=self.get_serializer_context()).data,
        )