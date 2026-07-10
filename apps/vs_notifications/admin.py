# =============================================================================
# vs_notifications / admin.py
#
# Django admin registrations for vs_notifications.
# These surfaces are used by Vision Staff for debugging and data inspection.
# They are not the primary template management interface (that is the API).
# =============================================================================

from django.contrib import admin

from .models import (
    Notification,
    NotificationEventType,
    NotificationSetting,
    NotificationTemplate,
)


# ---------------------------------------------------------------------------
# NotificationEventType
# ---------------------------------------------------------------------------

# Inspect the seeded event catalogue that drives notification dispatch.
@admin.register(NotificationEventType)
class NotificationEventTypeAdmin(admin.ModelAdmin):
    list_display  = ["key", "label", "source_module", "is_active", "is_transactional", "default_enabled"]
    list_filter   = ["source_module", "is_active", "is_transactional", "default_enabled"]
    search_fields = ["key", "label"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering      = ["source_module", "key"]

    fieldsets = (
        ("Identity", {
            "fields": ("id", "key", "label", "description", "source_module"),
        }),
        ("Channel & Defaults", {
            "fields": ("supported_channels", "default_enabled", "is_transactional", "is_active"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


# ---------------------------------------------------------------------------
# NotificationTemplate
# ---------------------------------------------------------------------------

# Inspect and repair templates outside the primary API management surface.
@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display  = ["event_type", "channel", "is_active", "updated_at", "updated_by"]
    list_filter   = ["channel", "is_active", "event_type__source_module"]
    search_fields = ["event_type__key", "subject", "body"]
    readonly_fields = ["id", "created_by", "updated_by", "created_at", "updated_at"]
    autocomplete_fields = ["event_type"]
    ordering = ["event_type__source_module", "event_type__key", "channel"]

    fieldsets = (
        ("Template", {
            "fields": ("id", "event_type", "channel", "is_active"),
        }),
        ("Content", {
            "fields": ("subject", "body", "html_body"),
            "description": (
                "Use {{ variable_name }} syntax for substitution. "
                "html_body (email only) makes delivery multipart when present. "
                "Available variables are defined per event type in the FRD."
            ),
        }),
        ("Audit", {
            "fields": ("created_by", "updated_by", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


# ---------------------------------------------------------------------------
# NotificationSetting
# ---------------------------------------------------------------------------

# Inspect platform and school overrides together for support/debugging.
@admin.register(NotificationSetting)
class NotificationSettingAdmin(admin.ModelAdmin):
    # school is nullable — a NULL school is a platform-wide default.
    list_display  = ["scope_label", "event_type", "channel", "is_enabled", "updated_at"]
    list_filter   = ["channel", "is_enabled", "event_type__source_module"]
    search_fields = ["school__name", "school__slug", "event_type__key"]
    readonly_fields = ["id", "updated_at"]
    ordering = ["school__name", "event_type__source_module", "event_type__key", "channel"]

    # Show platform-wide rows too — the tenant-aware default manager would hide
    # NULL-school rows outside a school context; use the unscoped manager.
    def get_queryset(self, request):
        return NotificationSetting.all_objects.select_related("school", "event_type")

    @admin.display(description="Scope", ordering="school__name")
    def scope_label(self, obj):
        return obj.school.name if obj.school_id else "— platform —"

    fieldsets = (
        ("Scope", {
            "fields": ("id", "school", "event_type", "channel"),
            "description": "Leave school blank for a platform-wide default.",
        }),
        ("Setting", {
            "fields": ("is_enabled", "updated_by", "updated_at"),
        }),
    )


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

# Inspect immutable delivery records and internal correlation metadata.
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display  = [
        "id", "event_type", "channel", "status",
        "recipient", "is_read", "retry_count", "created_at",
    ]
    list_filter   = ["channel", "status", "is_read", "event_type__source_module"]
    search_fields = [
        "recipient__email", "unregistered_email",
        "event_type__key", "subject",
    ]
    readonly_fields = [
        "id", "school", "recipient", "unregistered_email",
        "event_type", "channel", "subject", "body", "html_body", "metadata",
        "status", "failure_reason", "retry_count",
        "is_read", "read_at", "dispatched_at", "created_at",
    ]
    ordering = ["-created_at"]

    # Preserve the append-only delivery trail; corrections go through dispatch/API flows.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # Read-only in admin — modifications go through the API

    fieldsets = (
        ("Dispatch", {
            "fields": ("id", "school", "event_type", "channel", "status"),
        }),
        ("Recipient", {
            "fields": ("recipient", "unregistered_email"),
        }),
        ("Content", {
            "fields": ("subject", "body", "html_body"),
        }),
        ("Delivery", {
            "fields": ("retry_count", "failure_reason", "dispatched_at"),
        }),
        ("Internal", {
            "fields": ("metadata",),
            "classes": ("collapse",),
            "description": "Internal-only correlation data — never exposed via the API.",
        }),
        ("Read state", {
            "fields": ("is_read", "read_at"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )
