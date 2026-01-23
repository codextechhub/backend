# backend/apps/vision_admin_console/urls.py
from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AdminActionLogViewSet,
    DashboardViewSet,
    FeatureFlagViewSet,
    ImportJobLogViewSet,
    ImpersonationSessionViewSet,
    ProvisioningEventViewSet,
)

router = DefaultRouter()
router.register(r"admin-actions", AdminActionLogViewSet, basename="admin-actions")
router.register(r"feature-flags", FeatureFlagViewSet, basename="feature-flags")
router.register(r"provisioning-events", ProvisioningEventViewSet, basename="provisioning-events")
router.register(r"import-job-logs", ImportJobLogViewSet, basename="import-job-logs")
router.register(r"impersonations", ImpersonationSessionViewSet, basename="impersonations")
router.register(r"dashboard", DashboardViewSet, basename="dashboard")

urlpatterns = router.urls
