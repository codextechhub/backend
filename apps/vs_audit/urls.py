from django.urls import path

from .views import (
    AuditEventListView,
    AuditEventDetailView,
    AuditEventCreateView,
    EntityAuditTrailDetailView,
    AuditExportJobListView,
    AuditExportJobDetailView,
    AuditExportJobCreateView,
    ComplianceRuleListCreateView,
    ComplianceRuleDetailView,
)

urlpatterns = [
    # -------------------------------------------------------------------------
    # Audit Events
    # -------------------------------------------------------------------------
    path("events/", AuditEventListView.as_view(), name="audit-event-list"),
    path("events/create/", AuditEventCreateView.as_view(), name="audit-event-create"),
    path("events/<uuid:id>/", AuditEventDetailView.as_view(), name="audit-event-detail"),

    # -------------------------------------------------------------------------
    # Entity Trail
    # -------------------------------------------------------------------------
    path(
        "entity-trails/<str:entity_type>/<str:entity_id>/",
        EntityAuditTrailDetailView.as_view(),
        name="entity-audit-trail-detail",
    ),

    # -------------------------------------------------------------------------
    # Export Jobs
    # -------------------------------------------------------------------------
    path("exports/", AuditExportJobListView.as_view(), name="audit-export-list"),
    path("exports/create/", AuditExportJobCreateView.as_view(), name="audit-export-create"),
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