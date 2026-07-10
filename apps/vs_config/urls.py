from django.urls import path

from . import views


urlpatterns = [
    path("definitions/", views.DefinitionListCreateView.as_view(), name="config-definition-list"),
    path("definitions/<str:key>/", views.DefinitionDetailView.as_view(), name="config-definition-detail"),
    path("values/", views.ValueListSetView.as_view(), name="config-value-list"),
    path("effective-values/", views.EffectiveValueView.as_view(), name="config-effective-values"),
    path("effective-values/<str:key>/", views.EffectiveValueView.as_view(), name="config-effective-value"),
    path("capabilities/", views.CapabilityListCreateView.as_view(), name="config-capability-list"),
    path("capabilities/<slug:key>/", views.CapabilityDetailView.as_view(), name="config-capability-detail"),
    path("entitlements/", views.EntitlementListSetView.as_view(), name="config-entitlement-list"),
    path("overrides/", views.OverrideListSetView.as_view(), name="config-override-list"),
    path("effective-capabilities/", views.EffectiveCapabilitiesView.as_view(), name="config-effective-capabilities"),
    path("audit-events/", views.AuditEventListView.as_view(), name="config-audit-list"),
    path("export/", views.ConfigExportView.as_view(), name="config-export"),
]
