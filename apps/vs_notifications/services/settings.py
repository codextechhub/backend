# =============================================================================
# vs_notifications / services / settings.py
#
# Provides is_channel_enabled() — the single truth source for whether a
# given (school, event_type, channel) combination should fire.
#
# Called by dispatch.py before creating Notification records.
# Fails open: if no SchoolNotificationSetting record exists for a combination,
# dispatch proceeds as though it were enabled.  This prevents a missing seed
# from silently dropping notifications.
# =============================================================================

import logging

logger = logging.getLogger("vs_notifications.settings")


def is_channel_enabled(school, event_type, channel: str) -> bool:
    """
    Return True if the given channel should fire for the given school and
    event type.

    Logic (in order):
        1. If the event type's is_active flag is False at the platform level,
           return False immediately — school settings cannot override this.
        2. Look up the SchoolNotificationSetting for (school, event_type, channel).
        3. If no setting record exists, return True (fail-open default).
        4. Otherwise return the setting's is_enabled value.

    Args:
        school:      A School model instance.
        event_type:  A NotificationEventType model instance.
        channel:     A string from ChannelChoices (e.g. "in_app" or "email").
    """
    # Guard 1: Platform-level kill switch
    if not event_type.is_active:
        return False

    # Guard 2: Channel not declared as supported by this event type
    if channel not in event_type.supported_channels:
        return False

    # Import here to avoid circular imports at module load time
    from ..models import SchoolNotificationSetting

    try:
        setting = SchoolNotificationSetting.objects.get(
            school=school,
            event_type=event_type,
            channel=channel,
        )
        return setting.is_enabled
    except SchoolNotificationSetting.DoesNotExist:
        # Fail open: no seed record means treat as enabled
        logger.warning(
            "No SchoolNotificationSetting found for school=%s event_type=%s channel=%s. "
            "Defaulting to enabled. Run seed_notification_settings to fix.",
            school.slug if hasattr(school, "slug") else school.id,
            event_type.key,
            channel,
        )
        return True


def bulk_is_channel_enabled(school, event_type) -> dict[str, bool]:
    """
    Return a dict of {channel: is_enabled} for all supported channels of
    the given event type for the given school.

    More efficient than calling is_channel_enabled() N times when the
    dispatch service iterates over supported_channels.

    Args:
        school:      A School model instance.
        event_type:  A NotificationEventType model instance.
    """
    from ..models import SchoolNotificationSetting

    if not event_type.is_active:
        return {ch: False for ch in event_type.supported_channels}

    settings_qs = SchoolNotificationSetting.objects.filter(
        school=school,
        event_type=event_type,
        channel__in=event_type.supported_channels,
    ).values("channel", "is_enabled")

    settings_map = {row["channel"]: row["is_enabled"] for row in settings_qs}

    result = {}
    for channel in event_type.supported_channels:
        if channel not in settings_map:
            # Fail open — missing seed record
            logger.warning(
                "No SchoolNotificationSetting found for school=%s event_type=%s channel=%s.",
                school.slug if hasattr(school, "slug") else school.id,
                event_type.key,
                channel,
            )
            result[channel] = True
        else:
            result[channel] = settings_map[channel]

    return result
