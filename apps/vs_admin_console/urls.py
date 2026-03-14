from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardViewSet,
    ImpersonationSessionViewSet,
)

router = DefaultRouter()
router.register(r"impersonations", ImpersonationSessionViewSet, basename="impersonations")
router.register(r"dashboard", DashboardViewSet, basename="dashboard")

urlpatterns = router.urls
