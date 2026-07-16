from django.contrib import admin

from .models import (
    MonitoredService,
    Deployment,
    SLO,
    UptimeCheck,
    UptimeCheckResult,
    UptimeDailyRollup,
    QueueSnapshot,
    Incident,
    IncidentEvent,
    AlertRule,
    Alert,
    RequestMetric,
)


# Inspect monitored services and their current rolled-up health state.
@admin.register(MonitoredService)
class MonitoredServiceAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "group", "tier", "kind", "current_status", "is_active")
    list_filter = ("kind", "current_status", "is_active")
    search_fields = ("key", "name")


# Inspect configured probes that feed uptime results.
@admin.register(UptimeCheck)
class UptimeCheckAdmin(admin.ModelAdmin):
    list_display = ("name", "service", "check_type", "is_active", "interval_sec")
    list_filter = ("check_type", "is_active")


# Inspect alert thresholds that can auto-open incidents.
@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "metric", "comparator", "threshold", "severity", "is_enabled")
    list_filter = ("metric", "severity", "is_enabled")


# Show incident timeline events inline for war-room review.
class IncidentEventInline(admin.TabularInline):
    model = IncidentEvent
    extra = 0


# Inspect active and resolved operational incidents.
@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "severity", "status", "source", "started_at", "resolved_at")
    list_filter = ("severity", "status", "source")
    search_fields = ("code", "title")
    inlines = [IncidentEventInline]


# Inspect fired and resolved alerts emitted by rule evaluation.
@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "status", "fired_at", "resolved_at")
    list_filter = ("severity", "status")


# Inspect deployment annotations used on health timelines.
@admin.register(Deployment)
class DeploymentAdmin(admin.ModelAdmin):
    list_display = ("version", "kind", "environment", "actor", "deployed_at")
    list_filter = ("kind", "environment")


# Lightweight registrations for raw rollup tables used mainly for investigation.
admin.site.register(SLO)
admin.site.register(UptimeCheckResult)
admin.site.register(UptimeDailyRollup)
admin.site.register(QueueSnapshot)
admin.site.register(RequestMetric)
