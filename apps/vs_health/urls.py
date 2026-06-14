from django.urls import path

from .views import (
    OverviewView,
    ServiceListView,
    ServiceDetailView,
    UptimeMonitorsView,
    UptimeMonitorDetailView,
    ApiEndpointsView,
    ApiEndpointDetailView,
    QueuesView,
    TaskListView,
    IncidentListCreateView,
    IncidentDetailView,
    IncidentEventCreateView,
    ReliabilityView,
    AlertListView,
    AlertRuleListCreateView,
    AlertRuleDetailView,
    TenantListView,
    TenantDetailView,
    DeploymentListCreateView,
    SLOView,
)

urlpatterns = [
    # Command Center
    path("overview/", OverviewView.as_view(), name="health-overview"),

    # Services
    path("services/", ServiceListView.as_view(), name="health-service-list"),
    path("services/<slug:key>/", ServiceDetailView.as_view(), name="health-service-detail"),

    # Uptime & Availability
    path("uptime/monitors/", UptimeMonitorsView.as_view(), name="health-uptime-monitors"),
    path("uptime/monitors/<slug:key>/", UptimeMonitorDetailView.as_view(), name="health-uptime-monitor-detail"),

    # API & Endpoint Health
    path("api-endpoints/", ApiEndpointsView.as_view(), name="health-api-endpoints"),
    path("api-endpoints/detail/", ApiEndpointDetailView.as_view(), name="health-api-endpoint-detail"),

    # Background Jobs & Queues
    path("queues/", QueuesView.as_view(), name="health-queues"),
    path("tasks/", TaskListView.as_view(), name="health-tasks"),

    # Incidents & Alerts
    path("incidents/", IncidentListCreateView.as_view(), name="health-incident-list"),
    path("incidents/reliability/", ReliabilityView.as_view(), name="health-reliability"),
    path("incidents/<uuid:id>/", IncidentDetailView.as_view(), name="health-incident-detail"),
    path("incidents/<uuid:id>/events/", IncidentEventCreateView.as_view(), name="health-incident-event"),
    path("alerts/", AlertListView.as_view(), name="health-alerts"),
    path("alert-rules/", AlertRuleListCreateView.as_view(), name="health-alert-rule-list"),
    path("alert-rules/<uuid:id>/", AlertRuleDetailView.as_view(), name="health-alert-rule-detail"),

    # Tenant Health
    path("tenants/", TenantListView.as_view(), name="health-tenant-list"),
    path("tenants/<int:school_id>/", TenantDetailView.as_view(), name="health-tenant-detail"),

    # Deployments & SLOs
    path("deployments/", DeploymentListCreateView.as_view(), name="health-deployment-list"),
    path("slos/", SLOView.as_view(), name="health-slos"),
]
