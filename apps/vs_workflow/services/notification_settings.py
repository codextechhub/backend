"""Whether a school's approvals tell people what is happening.

One switch for the whole school, rather than four questions on every template.
The template screen used to ask which lifecycle events should notify, which was
both too many decisions to take per path and a trap: an untouched template
notified on everything, while setting any one event made the other three count
as off - so a school could turn off "rejected - notify the requester" without
ever meaning to.

The value lives in the ``vs_config`` catalogue (migration 0011), which already
resolves through platform and school scope and already keeps an audit trail.
Reading falls back to notifying, so a platform whose catalogue has not been
seeded behaves exactly as it did before this existed.
"""
from vs_workflow.constants import CFG_NOTIFICATIONS_ENABLED
from vs_workflow.exceptions import NotificationSettingNotRegistered

#: What the engine does when a school has chosen nothing.
DEFAULT_ENABLED = True


def notifications_enabled(tenant) -> bool:
    """Whether *tenant* wants its approvals to notify anybody."""
    from vs_config.conf import get_config

    if tenant is None:
        return DEFAULT_ENABLED
    return bool(get_config(CFG_NOTIFICATIONS_ENABLED, default=DEFAULT_ENABLED, tenant=tenant))


def set_notifications_enabled(tenant, actor, *, enabled: bool) -> bool:
    """Record *tenant*'s answer, and return it.

    Raises :class:`~vs_workflow.exceptions.NotificationSettingNotRegistered`
    when the catalogue has no such definition, because a write that stores
    nothing and reports success is worse than a refusal.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import set_value

    definition = ConfigurationDefinition.objects.filter(
        key=CFG_NOTIFICATIONS_ENABLED, is_active=True,
    ).first()
    if definition is None:
        raise NotificationSettingNotRegistered(key=CFG_NOTIFICATIONS_ENABLED)
    set_value(
        definition=definition, value=bool(enabled), actor=actor, tenant=tenant,
        reason="Workflow notifications set from the Workflow area.",
    )
    return bool(enabled)
