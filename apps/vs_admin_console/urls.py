from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ConsoleOverviewView,
    DashboardViewSet,
    ImpersonationSessionViewSet,
)
from .views_tasks import TaskMonitorViewSet

router = DefaultRouter()
router.register(r"impersonations", ImpersonationSessionViewSet, basename="impersonations")
router.register(r"dashboard", DashboardViewSet, basename="dashboard")
router.register(r"tasks", TaskMonitorViewSet, basename="tasks")

urlpatterns = [
    # Declared ahead of the router: `dashboard/` is a registered basename, and a
    # router lookup would otherwise read "overview" as a detail pk.
    path("dashboard/overview/", ConsoleOverviewView.as_view(), name="console-overview"),
    *router.urls,
]
