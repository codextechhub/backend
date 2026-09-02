from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ConsoleOverviewView,
    DashboardViewSet,
    ImpersonationSessionViewSet,
)
from .views_documents import (
    RequirementsDocumentDownloadView,
    RequirementsDocumentListView,
)
from .views_finance_inventory import FinanceEntityInventoryView
from .views_tasks import TaskMonitorViewSet

router = DefaultRouter()
router.register(r"impersonations", ImpersonationSessionViewSet, basename="impersonations")
router.register(r"dashboard", DashboardViewSet, basename="dashboard")
router.register(r"tasks", TaskMonitorViewSet, basename="tasks")

urlpatterns = [
    # Declared ahead of the router: `dashboard/` is a registered basename, and a
    # router lookup would otherwise read "overview" as a detail pk.
    path("dashboard/overview/", ConsoleOverviewView.as_view(), name="console-overview"),
    # Who has books, not what is in them. Reading a school's figures still goes
    # through proxying, so it stays attributable to somebody entitled to them.
    path(
        "finance/entities/",
        FinanceEntityInventoryView.as_view(),
        name="console-finance-entity-inventory",
    ),
    # The requirements-document library. Not a router registration: it is backed
    # by the filesystem rather than a queryset, and only ever reads.
    path(
        "documents/",
        RequirementsDocumentListView.as_view(),
        name="requirements-documents",
    ),
    path(
        "documents/<slug:slug>/download/",
        RequirementsDocumentDownloadView.as_view(),
        name="requirements-document-download",
    ),
    *router.urls,
]
