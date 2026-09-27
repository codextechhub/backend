"""The administrative changes to notifications that must leave a record.

Two things an administrator changes here decide what people receive. A
notification setting switches a channel on or off for one event, for one tenant
or, at the platform layer, for every tenant that has not chosen for itself. A
template is the wording of a message, and it is global: one row per event and
channel, sent to every tenant on the platform. A bursar who stops getting
payment emails, or a school whose invoice email suddenly reads differently,
needs to be able to find out who changed it and when.

Every such write goes through this module into the platform audit trail
(:func:`vs_audit.services.emit_audit_event`) as ``CONFIG`` / ``CONFIG_CHANGED``,
the same pair ``vs_config`` mirrors its own value changes under, because both
are configuration of the platform's behaviour. Whether a row was created or
edited is in the summary and the diff (a created row has ``before: None``).
Audit is best effort by the audit app's own contract: a failure to record never
undoes the change.
"""
from __future__ import annotations

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import AuditDiffService, emit_audit_event

#: The template columns an administrator edits. Timestamps and editor columns
#: are left out: they change on every save and say nothing about the message.
TEMPLATE_AUDIT_FIELDS = (
    "subject", "body", "cta_label", "cta_url", "html_body", "html_is_custom",
    "is_active",
)


def record_setting_change(*, actor, tenant, event_type, channel, before, after):
    """Record one setting row switched, created or re-pinned at one layer.

    *before* is the value stored at this layer before the write, or ``None``
    when the layer had no row and the value was inherited. *tenant* ``None``
    is the platform default layer. A write that leaves the stored value as it
    was is not a change and records nothing.
    """
    if before == after:
        return None
    layer = "platform default" if tenant is None else "tenant"
    state = "on" if after else "off"
    return emit_audit_event(
        module_key=AuditModuleKey.CONFIG,
        action_type=AuditActionType.CONFIG_CHANGED,
        entity_type="NotificationSetting",
        entity_id=f"{event_type.key}:{channel}",
        entity_label=f"{event_type.label} ({channel})",
        actor_user=actor,
        tenant=tenant,
        summary=f"Notification {event_type.key} by {channel} turned {state} ({layer})",
        before_data={"is_enabled": before},
        diff_data=AuditDiffService.diff_dicts(
            {"is_enabled": before}, {"is_enabled": after},
        ),
        metadata={
            "event_type_key": event_type.key,
            "channel": channel,
            "layer": "platform" if tenant is None else "tenant",
            "created": before is None,
        },
    )


def template_snapshot(template) -> dict:
    """The audited columns of *template*, for a before/after pair."""
    return {field: getattr(template, field, None) for field in TEMPLATE_AUDIT_FIELDS}


def record_template_change(*, actor, template, before=None):
    """Record a template created (*before* ``None``) or edited.

    An edit that changed none of the audited columns records nothing. The
    tenant is left to the request: templates are global, so the event belongs to
    whoever made the change, which is the platform.
    """
    after = template_snapshot(template)
    diff = AuditDiffService.diff_dicts(before or {}, after)
    if before is not None and not diff:
        return None
    label = f"{template.event_type.key} ({template.channel})"
    created = before is None
    return emit_audit_event(
        module_key=AuditModuleKey.CONFIG,
        action_type=AuditActionType.CONFIG_CHANGED,
        entity_type="NotificationTemplate",
        entity_id=str(template.pk),
        entity_label=label,
        actor_user=actor,
        summary=f"Notification template {label} {'created' if created else 'edited'}",
        before_data={field: (before or {}).get(field) for field in diff} if not created else {},
        diff_data=diff,
        metadata={
            "event_type_key": template.event_type.key,
            "channel": template.channel,
            "created": created,
        },
    )
