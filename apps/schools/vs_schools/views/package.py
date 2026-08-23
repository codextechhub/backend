# views/package.py

from rest_framework import generics
from ..models import PackagePlan
from vs_config.models import Capability
from vs_rbac.permissions import IsVisionStaff, IsAuthenticatedAndActive
from ..serializers import PackagePlanSerializer, XVSModuleSerializer


class PackagePlanListView(generics.ListAPIView):
    """
    Returns all active PackagePlans.
    Powers the 'Select package plan' dropdown on the UI.

    docstring-name: Package plans
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = PackagePlanSerializer
    queryset = PackagePlan.objects.filter(is_active=True).order_by("name")


class XVSModuleListView(generics.ListAPIView):
    """
    Returns all active platform modules.
    Powers the 'Select modules' multi-select dropdown on the UI.

    docstring-name: XVS modules
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = XVSModuleSerializer
    queryset = (
        Capability.objects.filter(is_active=True, kind=Capability.Kind.MODULE)
        # Prefetch backs the serializer's dependency-key read without N+1.
        .prefetch_related("dependency_links__requires")
        .order_by("label")
    )
