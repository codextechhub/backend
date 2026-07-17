# =============================================================================
# vs_notifications / urls.py
#
# URL routing for vs_notifications.
# All routes are prefixed with /api/v1/notifications/ by the root urls.py.
#
# Route summary:
#   /notifications/                         — feed list (GET)
#   /notifications/<uuid>/                  — feed detail (GET)
#   /notifications/unread-count/            — unread count (GET)
#   /notifications/mark-read/               — mark list as read (POST)
#   /notifications/mark-all-read/           — mark all as read (POST)
#   /notifications/acknowledge-route/       — mark viewed destination events (POST)
#   /notifications/history/                 — admin history list (GET)
#   /notifications/history/<uuid>/          — admin history detail (GET)
#   /notifications/settings/               — effective settings matrix (GET)
#   /notifications/settings/update/        — settings override upsert (PATCH)
#   /notifications/templates/               — template list (GET) / create (POST)
#   /notifications/templates/<uuid>/        — template retrieve (GET) / update (PATCH)
#   /notifications/templates/<uuid>/preview/— template preview (POST)
#   /notifications/event-types/             — event type list (GET)
#   /notifications/event-types/<uuid>/      — event type retrieve (GET)
# =============================================================================

from django.urls import path

from .views import (
    NotificationEventTypeViewSet,
    NotificationHistoryViewSet,
    NotificationSettingViewSet,
    NotificationTemplateViewSet,
    NotificationViewSet,
)

# ── Feed endpoints (user-facing) ─────────────────────────────────────────────
feed_list   = NotificationViewSet.as_view({"get": "list"})
feed_detail = NotificationViewSet.as_view({"get": "retrieve"})

# ── History endpoints (admin) ─────────────────────────────────────────────────
history_list   = NotificationHistoryViewSet.as_view({"get": "list"})
history_detail = NotificationHistoryViewSet.as_view({"get": "retrieve"})

# ── Settings endpoints (school admin + CX staff) ──────────────────────────────
# GET returns the EFFECTIVE matrix for the caller's scope; PATCH upserts overrides
# by (event_type_key, channel). CX staff can pass ?school=<id> to target a school.
settings_list   = NotificationSettingViewSet.as_view({"get": "list"})
settings_update = NotificationSettingViewSet.as_view({"patch": "partial_update"})

# ── Template endpoints (Vision Staff) ─────────────────────────────────────────
template_list   = NotificationTemplateViewSet.as_view({"get": "list", "post": "create"})
template_detail = NotificationTemplateViewSet.as_view({"get": "retrieve", "patch": "partial_update"})

# ── Event type endpoints (all authenticated) ──────────────────────────────────
event_type_list   = NotificationEventTypeViewSet.as_view({"get": "list"})
event_type_detail = NotificationEventTypeViewSet.as_view({"get": "retrieve"})

urlpatterns = [

    # Feed
    path("",                   feed_list,   name="notification-list"),
    path("<uuid:pk>/",         feed_detail, name="notification-detail"),
    path(
        "unread-count/",
        NotificationViewSet.as_view({"get": "unread_count"}),
        name="notification-unread-count",
    ),
    path(
        "mark-read/",
        NotificationViewSet.as_view({"post": "mark_read"}),
        name="notification-mark-read",
    ),
    path(
        "mark-all-read/",
        NotificationViewSet.as_view({"post": "mark_all_read"}),
        name="notification-mark-all-read",
    ),
    path(
        "acknowledge-route/",
        NotificationViewSet.as_view({"post": "acknowledge_route"}),
        name="notification-acknowledge-route",
    ),

    # History
    path("history/",           history_list,   name="notification-history-list"),
    path("history/<uuid:pk>/", history_detail, name="notification-history-detail"),

    # Settings
    path("settings/",        settings_list,   name="notification-settings-list"),
    path("settings/update/", settings_update, name="notification-settings-update"),

    # Templates
    path("templates/",           template_list,   name="notification-template-list"),
    path("templates/<uuid:pk>/", template_detail, name="notification-template-detail"),
    path(
        "templates/<uuid:pk>/preview/",
        NotificationTemplateViewSet.as_view({"post": "preview"}),
        name="notification-template-preview",
    ),

    # Event types
    path("event-types/",           event_type_list,   name="notification-event-type-list"),
    path("event-types/<uuid:pk>/", event_type_detail, name="notification-event-type-detail"),
]
