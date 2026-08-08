"""URL map for the Export Centre - mounted at ``/v1/exports/``.

Routes mirror the capabilities the handoff lists (D10·1): catalogue, preview and
estimate, quick export, definitions, runs, files, deliveries, capabilities,
activity.
Every list route is paginated and every mutation is RBAC-gated in the view.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Catalogue and capabilities
    path("catalogue/", views.CatalogueView.as_view(), name="export-catalogue"),
    path("catalogue/<str:key>/", views.CatalogueDetailView.as_view(), name="export-catalogue-detail"),
    path("capabilities/", views.CapabilitiesView.as_view(), name="export-capabilities"),

    # Preview and estimate
    path("preview/", views.PreviewView.as_view(), name="export-preview"),

    # Quick export - run a configuration that was never saved
    path("quick/", views.QuickExportView.as_view(), name="export-quick"),

    # Definitions
    path("definitions/", views.DefinitionListView.as_view(), name="export-definitions"),
    path("definitions/<int:pk>/", views.DefinitionDetailView.as_view(), name="export-definition-detail"),
    path("definitions/<int:pk>/duplicate/", views.DefinitionDuplicateView.as_view(), name="export-definition-duplicate"),
    path("definitions/<int:pk>/share/", views.DefinitionShareView.as_view(), name="export-definition-share"),
    path("definitions/<int:pk>/run/", views.DefinitionRunView.as_view(), name="export-definition-run"),

    # Runs
    path("runs/", views.RunListView.as_view(), name="export-runs"),
    path("runs/<int:pk>/", views.RunDetailView.as_view(), name="export-run-detail"),
    path("runs/<int:pk>/cancel/", views.RunCancelView.as_view(), name="export-run-cancel"),
    path("runs/<int:pk>/retry/", views.RunRetryView.as_view(), name="export-run-retry"),

    # Files
    path("files/", views.FileListView.as_view(), name="export-files"),
    path("files/<int:pk>/download/", views.FileDownloadView.as_view(), name="export-file-download"),
    path("files/<int:pk>/downloads/", views.FileDownloadLogView.as_view(), name="export-file-downloads"),

    # Product analytics - a separate pipeline from the audit trail
    path("analytics/", views.AnalyticsIngestView.as_view(), name="export-analytics"),
    path("analytics/summary/", views.AnalyticsSummaryView.as_view(), name="export-analytics-summary"),

    # Deliveries and admin activity
    path("deliveries/<int:pk>/revoke/", views.DeliveryRevokeView.as_view(), name="export-delivery-revoke"),
    path("activity/", views.ActivityView.as_view(), name="export-activity"),
]
