"""The notification screens and settings the search bar can route to.

Each event a tenant may switch on or off is one setting on the notification
settings screen, anchored at its event key, in the words of
:data:`vs_notifications.constants.EVENT_TYPE_REGISTRY` and grouped by the area
that raises it (:func:`vs_notifications.labels.source_module_label`). A
transactional event is left out: it is always sent and no scope may change it,
so there is nothing on the screen to route to.

The ``communication`` permission module answers to the ``email_alerts``
capability (:mod:`vs_rbac.capability_map`).
"""
from __future__ import annotations

from vs_search.registry import ACTION, SETTING, Place, register

from .constants import EVENT_TYPE_REGISTRY, NotificationPermission
from .labels import source_module_label

CAPABILITY = "email_alerts"
SETTINGS_SCREEN = "notifications.settings"


def places():
    """The notification screens, then each switchable event."""
    # Every member reads their own inbox, whatever the plan includes.
    yield Place(
        kind=ACTION, key="notifications.inbox", label="Notifications",
        description="Your own notifications, newest first.",
        keywords=("inbox", "alerts", "unread"),
        screen="notifications.inbox",
    )
    yield Place(
        kind=ACTION, key=SETTINGS_SCREEN, label="Notification settings",
        description="Which events send an email or an in-app notification.",
        keywords=("email", "alerts"),
        screen=SETTINGS_SCREEN, permission=NotificationPermission.ENFORCE_PERMISSIONS,
        capability=CAPABILITY,
    )
    yield Place(
        kind=ACTION, key="notifications.templates", label="Notification templates",
        description="The wording of each notification.",
        keywords=("email",),
        screen="notifications.templates",
        permission=NotificationPermission.TEMPLATE_CONFIGURE, capability=CAPABILITY,
    )
    yield Place(
        kind=ACTION, key="notifications.history", label="Notification history",
        description="Every notification sent and whether it was delivered.",
        keywords=("email", "delivery"),
        screen="notifications.history",
        permission=NotificationPermission.AUDIT_ACTIVITY, capability=CAPABILITY,
    )
    for event in EVENT_TYPE_REGISTRY:
        if event.get("is_transactional"):
            continue
        yield Place(
            kind=SETTING,
            key=f"{SETTINGS_SCREEN}.{event['key']}",
            label=event["label"],
            description=event.get("description", ""),
            keywords=("notification",),
            group=f"Notifications: {source_module_label(event.get('source_module'))}",
            screen=SETTINGS_SCREEN,
            anchor=event["key"],
            permission=NotificationPermission.ENFORCE_PERMISSIONS,
            capability=CAPABILITY,
        )


register(*places())
