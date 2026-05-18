from django.urls import path

from .views import (
    AuditEventListView,
    AuditEventDetailView,
    AuditDashboardSummaryView,
    EntityAuditTrailListView,
    EntityAuditTrailDetailView,
    MyActivityView,
    AuditExportJobListView,
    AuditExportJobDetailView,
    ComplianceRuleListCreateView,
    ComplianceRuleDetailView,
)

urlpatterns = [
    # -------------------------------------------------------------------------
    # Security Dashboard summary
    # -------------------------------------------------------------------------
    path("dashboard-summary/", AuditDashboardSummaryView.as_view(), name="audit-dashboard-summary"),

    # -------------------------------------------------------------------------
    # Audit Events
    # -------------------------------------------------------------------------
    path("events/", AuditEventListView.as_view(), name="audit-event-list"),
    path("events/<uuid:id>/", AuditEventDetailView.as_view(), name="audit-event-detail"),

    # -------------------------------------------------------------------------
    # Entity Trails
    # -------------------------------------------------------------------------
    path("entity-trails/", EntityAuditTrailListView.as_view(), name="entity-audit-trail-list"),
    path(
        "entity-trails/<str:entity_type>/<str:entity_id>/",
        EntityAuditTrailDetailView.as_view(),
        name="entity-audit-trail-detail",
    ),

    # -------------------------------------------------------------------------
    # Self-service /me
    # -------------------------------------------------------------------------
    path("me/activity/", MyActivityView.as_view(), name="audit-me-activity"),

    # -------------------------------------------------------------------------
    # Export Jobs
    # -------------------------------------------------------------------------
    path("exports/", AuditExportJobListView.as_view(), name="audit-export-list"),
    path("exports/<uuid:id>/", AuditExportJobDetailView.as_view(), name="audit-export-detail"),

    # -------------------------------------------------------------------------
    # Compliance Rules
    # -------------------------------------------------------------------------
    path(
        "compliance-rules/",
        ComplianceRuleListCreateView.as_view(),
        name="compliance-rule-list-create",
    ),
    path(
        "compliance-rules/<uuid:id>/",
        ComplianceRuleDetailView.as_view(),
        name="compliance-rule-detail",
    ),
]