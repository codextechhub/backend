from django.urls import path

from . import views


urlpatterns = [
    path("definitions/", views.DefinitionListCreateView.as_view(), name="config-definition-list"),
    path("definitions/<str:key>/", views.DefinitionDetailView.as_view(), name="config-definition-detail"),
    path("values/", views.ValueListSetView.as_view(), name="config-value-list"),
    path("values/<str:key>/", views.ValueResetView.as_view(), name="config-value-reset"),
    path("platform-settings/", views.PlatformSettingsView.as_view(), name="platform-settings"),
    path("security-settings/", views.SecuritySettingsView.as_view(), name="security-settings"),
    path("integration-settings/", views.IntegrationSettingsView.as_view(), name="integration-settings"),
    path("integration-settings/test/", views.IntegrationConnectionTestView.as_view(), name="integration-settings-test"),
    path("effective-values/", views.EffectiveValueView.as_view(), name="config-effective-values"),
    path("effective-values/<str:key>/", views.EffectiveValueView.as_view(), name="config-effective-value"),
    path("capabilities/", views.CapabilityListCreateView.as_view(), name="config-capability-list"),
    path("capabilities/<slug:key>/", views.CapabilityDetailView.as_view(), name="config-capability-detail"),
    path("entitlements/", views.EntitlementListSetView.as_view(), name="config-entitlement-list"),
    path("entitlements/calendar/", views.EntitlementCalendarView.as_view(), name="config-entitlement-calendar"),
    path("entitlements/bulk-schedule/", views.EntitlementBulkScheduleView.as_view(), name="config-entitlement-bulk-schedule"),
    path("entitlements/<slug:capability>/", views.EntitlementResetView.as_view(), name="config-entitlement-reset"),
    path("overrides/", views.OverrideListSetView.as_view(), name="config-override-list"),
    path("effective-capabilities/", views.EffectiveCapabilitiesView.as_view(), name="config-effective-capabilities"),
    path("audit-events/", views.AuditEventListView.as_view(), name="config-audit-list"),
    path("audit-events/facets/", views.AuditEventFacetsView.as_view(), name="config-audit-facets"),
    path("audit-events/export/", views.AuditEventExportView.as_view(), name="config-audit-export"),
    path("audit-events/saved-views/", views.AuditSavedViewListCreateView.as_view(), name="config-audit-saved-views"),
    path("audit-events/saved-views/<uuid:view_id>/", views.AuditSavedViewDeleteView.as_view(), name="config-audit-saved-view-delete"),
    path("audit-events/export-jobs/", views.AuditExportJobListCreateView.as_view(), name="config-audit-export-jobs"),
    path("audit-events/export-jobs/<uuid:job_id>/download/", views.AuditExportJobDownloadView.as_view(), name="config-audit-export-job-download"),
    path("audit-events/<uuid:event_id>/", views.AuditEventDetailView.as_view(), name="config-audit-detail"),
    path("export/", views.ConfigExportView.as_view(), name="config-export"),
]
