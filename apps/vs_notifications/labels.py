"""The words a person reads for the machine values the notifications API returns.

An event type's ``source_module`` is the app that raises it (``vs_procurement``),
a channel is ``in_app`` or ``email``, a delivery status is ``SENT``, and a
setting's ``source`` is the layer that decided it (``tenant``). Those values
stay as stored, because filters and ordering rely on them. Every response that
carries one also carries its ``*_label`` from here, so no client turns an app
name or an enum member into a heading.

Each lookup falls back to a neutral phrase, never to the raw value.
"""

from .constants import ChannelChoices, NotificationStatus

#: The area of the product an event belongs to, keyed by the app that raises it.
SOURCE_MODULE_LABELS = {
    "core": "Platform",
    "vs_billing": "Billing",
    "vs_exports": "Exports",
    "vs_finance": "Finance",
    "vs_health": "Health",
    "vs_import": "Imports",
    "vs_notifications": "Notifications",
    "vs_onboarding": "Onboarding",
    "vs_payments": "Payments",
    "vs_procurement": "Procurement",
    "vs_students": "Students",
    "vs_tickets": "Support tickets",
    "vs_todo": "Tasks",
    "vs_user": "Accounts",
    "vs_users": "Accounts",
    "vs_workflow": "Approvals",
}
SOURCE_MODULE_FALLBACK = "Other notifications"

#: Which settings layer produced an effective on/off value.
SETTING_SOURCE_LABELS = {
    "branch": "Branch setting",
    "tenant": "Own setting",
    "platform": "Platform default",
    "default": "Built-in default",
}
SETTING_SOURCE_FALLBACK = "Built-in default"

CHANNEL_LABELS = dict(ChannelChoices.CHOICES)
STATUS_LABELS = dict(NotificationStatus.CHOICES)


def source_module_label(source_module):
    """The product area an event's ``source_module`` names, in words."""
    return SOURCE_MODULE_LABELS.get(source_module, SOURCE_MODULE_FALLBACK)


def setting_source_label(source):
    """The settings layer that decided a value, in words."""
    return SETTING_SOURCE_LABELS.get(source, SETTING_SOURCE_FALLBACK)


def channel_label(channel):
    """A delivery channel in words ("In-App", "Email")."""
    return CHANNEL_LABELS.get(channel, "Notification")


def status_label(status):
    """A delivery status in words ("Pending", "Sent", "Failed")."""
    return STATUS_LABELS.get(status, "Pending")
