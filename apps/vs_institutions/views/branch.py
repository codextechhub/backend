from __future__ import annotations

from django.db.models import Q
from django.forms import ValidationError
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from ..models import Branch, BranchStatus, Institution
from ..permissions import IsVisionStaff
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
    permission_classes = [IsVisionStaff]
    serializer_class = BranchListSerializer
    queryset = Branch.objects.all().select_related("institution")

    def get_queryset(self):
        qs = super().get_queryset()

        # Filter by institution (tenant)
        qs = qs.filter(institution__slug=self.kwargs.get("slug"))

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
                | Q(institution__name__icontains=q)
                | Q(institution__slug__icontains=q)
            )

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name", "code", "-code"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("created_at")
        return qs


class BranchStatsView(generics.GenericAPIView):
    """
    Returns a single summary payload with branch counts broken down by status.
    Designed for the Branch Management dashboard stat cards.

    Supports optional institution scoping via ?s=<institution_slug>

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
    permission_classes = [IsVisionStaff]

    def get(self, request, *args, **kwargs):
        from django.db.models import Count, Q

        qs = Branch.objects.all()

        i_slug = self.kwargs.get("slug")
        qs = qs.filter(institution__slug=i_slug)

        result = qs.aggregate(
            all=Count("id"),
            active=Count("id", filter=Q(status=BranchStatus.ACTIVE)),
            pending=Count("id", filter=Q(status=BranchStatus.PENDING)),
            suspended=Count("id", filter=Q(status=BranchStatus.SUSPENDED)),
            inactive=Count("id", filter=Q(status=BranchStatus.INACTIVE)),
            closed=Count("id", filter=Q(status=BranchStatus.CLOSED)),
        )

        return Response(result)
    

class BranchCreateView(ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = BranchCreateSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        i_slug = self.kwargs["i_slug"]

        if not Institution.objects.filter(slug=i_slug).exists():
            raise ValueError(f"Institution with slug '{i_slug}' does not exist.")
        
        if Institution.objects.filter(slug=i_slug).first().status != "ACTIVE":
            raise ValidationError({"detail": "Branches can only be created for active institutions."})

        ctx["institution"] = Institution.objects.filter(slug=i_slug).first()
        return ctx


class BranchDetailView(ActorContextMixin, generics.RetrieveAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = BranchDetailSerializer

    queryset = Branch.objects.all().select_related("institution")
    lookup_field = "code"

    def get_queryset(self):
        """
        Optional extra safety:
        If institution slug is in the URL, scope branch lookup to that institution.
        """
        qs = super().get_queryset()
        i_slug = self.kwargs.get("i_slug")   # "s" for "slug" to keep it short in URL
        if i_slug:
            qs = qs.filter(institution__slug=i_slug)
        return qs


class BranchUpdateView(ActorContextMixin, generics.UpdateAPIView):
    """
    Returns a full detail payload after update so the UI doesn't need to refetch.
    """
    permission_classes = [IsVisionStaff]
    serializer_class = BranchUpdateSerializer

    queryset = Branch.objects.all().select_related("institution")
    lookup_field = "code"

    def get_queryset(self):
        qs = super().get_queryset()
        i_slug = self.kwargs.get("i_slug")  # "s" for "slug" to keep it short in URL
        if i_slug:
            qs = qs.filter(institution__slug=i_slug)
        return qs

    def update(self, request, *args, **kwargs):
        resp = super().update(request, *args, **kwargs)
        branch = self.get_object()
        return Response(BranchDetailSerializer(branch, context=self.get_serializer_context()).data)