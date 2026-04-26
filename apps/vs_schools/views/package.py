# views/package.py

from rest_framework import generics
from ..models import PackagePlan, XVSModules
from vs_rbac.permissions import IsVisionStaff, IsAuthenticatedAndActive
from ..serializers import PackagePlanSerializer, XVSModuleSerializer


class PackagePlanListView(generics.ListAPIView):
    """
    Returns all active PackagePlans.
    Powers the 'Select package plan' dropdown on the UI.
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = PackagePlanSerializer
    queryset = PackagePlan.objects.filter(is_active=True).order_by("name")


class XVSModuleListView(generics.ListAPIView):
    """
    Returns all active platform modules.
    Powers the 'Select modules' multi-select dropdown on the UI.
    """
    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff]
    serializer_class = XVSModuleSerializer
    queryset = XVSModules.objects.filter(is_active=True).order_by("name")