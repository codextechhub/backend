from __future__ import annotations

from django.db.models import Q
from rest_framework import generics
from rest_framework.response import Response

from ..models import Branch, BranchStatus
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
        user = getattr(self.request, "user", None)
        ctx["actor_id"] = str(getattr(user, "id", "system"))
        return ctx


class BranchListView(ActorContextMixin, generics.ListAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = BranchListSerializer

    queryset = Branch.objects.all().select_related("institution")

    def get_queryset(self):
        qs = super().get_queryset()

        # Filter by institution (tenant)
        institution_slug = (self.request.query_params.get("institution") or "").strip()
        if institution_slug:
            qs = qs.filter(institution__slug=institution_slug)

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
                | Q(city__icontains=q)
                | Q(country__icontains=q)
                | Q(email__icontains=q)
                | Q(phone_number__icontains=q)
                | Q(institution__name__icontains=q)
                | Q(institution__slug__icontains=q)
            )

        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed = {"created_at", "-created_at", "updated_at", "-updated_at", "name", "-name", "code", "-code"}
        qs = qs.order_by(ordering) if ordering in allowed else qs.order_by("created_at")
        return qs


class BranchCountView(generics.GenericAPIView):
    permission_classes = [IsVisionStaff]

    def get(self, request, *args, **kwargs):
        qs = Branch.objects.all()

        i_slug = (self.request.query_params.get("s") or "").strip() # "s" for "slug" to keep it short in URL
        if i_slug:
            qs = qs.filter(institution__slug=i_slug)

        param = "all"
        count = qs.count()

        # status selectors
        for key, value in [
            ("active", BranchStatus.ACTIVE),
            ("inactive", BranchStatus.INACTIVE),
            ("suspended", BranchStatus.SUSPENDED),
            ("pending", BranchStatus.PENDING),
            ("closed", BranchStatus.CLOSED),
        ]:
            flag = (self.request.query_params.get(key) or "").strip().lower()
            if flag in ("1", "true", "yes"):
                param = key
                count = qs.filter(status=value).count()
                break

        return Response({f"{param.capitalize()} count": count})


class BranchCreateView(ActorContextMixin, generics.CreateAPIView):
    permission_classes = [IsVisionStaff]
    serializer_class = BranchCreateSerializer


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
        institution_slug = self.kwargs.get("institution_slug")
        if institution_slug:
            qs = qs.filter(institution__slug=institution_slug)
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
        institution_slug = self.kwargs.get("institution_slug")
        if institution_slug:
            qs = qs.filter(institution__slug=institution_slug)
        return qs

    def update(self, request, *args, **kwargs):
        resp = super().update(request, *args, **kwargs)
        branch = self.get_object()
        return Response(BranchDetailSerializer(branch, context=self.get_serializer_context()).data)