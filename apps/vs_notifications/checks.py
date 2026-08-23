# =============================================================================
# vs_notifications / checks.py
#
# One Django system check, registered from VsNotificationsConfig.ready().
#
# WHY IT EXISTS
# -------------
# services/dispatch._fetch_templates returns nothing for a channel that has no
# ACTIVE NotificationTemplate, and the dispatch loop then logs a WARNING and
# skips that channel. Nothing raises, nothing retries, nothing fails. An
# environment where the templates were never seeded therefore sends NOTHING,
# and the only evidence is a log line nobody reads.
#
# This check reports that state at check time instead. It deliberately does NOT
# change dispatch: a missing template still skips rather than raises, because
# making a live send path fail hard is a separate and riskier decision.
#
# WHY THE DATABASE TAG
# --------------------
# The check queries the database, so it must not run on every management
# command. Tagged with Tags.database it is skipped by a tag-filtered run, and
# the `databases` guard below means it does no query at all unless the caller
# actually named a database. Untagged and unguarded it would execute during
# collectstatic, during migrate on an empty database, and in any environment
# where the database is unreachable, turning a working command into a crash.
# =============================================================================

from django.core.checks import Tags, Warning as CheckWarning, register
from django.db.utils import OperationalError, ProgrammingError

# Stable id so an environment that has accepted a gap can silence exactly this.
CHECK_ID = "vs_notifications.W001"


@register(Tags.database)
def check_event_types_have_templates(app_configs=None, databases=None, **kwargs):
    """
    Report every ACTIVE event type with no ACTIVE template on a channel it
    declares in supported_channels.

    "Meant to deliver on that channel" is exactly what seeding uses: an active
    NotificationEventType, and each entry of its supported_channels. Inactive
    event types are never dispatched (the platform kill switch wins over
    settings, see services/settings.resolve_channels), so they are not reported.
    Channel enablement is deliberately NOT consulted: a NotificationSetting is
    per tenant, so a channel switched off platform-wide can still be switched on
    by any one tenant, and the missing template would bite them.

    How to run it:

        python manage.py check --database default

    That is the command for an operator or a deploy step. A plain
    `manage.py check` names no database, so this check returns immediately
    without querying. The Django test runner passes its databases, so the suite
    runs it too.
    """
    # No database named: do not touch the connection. This is what keeps
    # collectstatic and friends from paying for (or crashing on) a query.
    if not databases:
        return []

    messages = []
    for alias in databases:
        gaps = _missing_templates(alias)
        if gaps:
            messages.append(
                _build_warning(gaps, alias, name_database=len(databases) > 1)
            )
    return messages


# Collect the (event key, channel) pairs that would silently send nothing.
def _missing_templates(alias):
    """
    Return a sorted list of (event_key, channel) pairs with no active template.

    Returns an empty list when the notification tables are not there yet, or the
    database cannot be reached. A fresh database has no tables until migrate has
    run, and a check that breaks migrate on a new environment would be worse
    than the problem it reports.
    """
    from .models import NotificationEventType, NotificationTemplate
    try:
        declared = list(
            NotificationEventType.objects.using(alias)
            .filter(is_active=True)
            .values_list("key", "supported_channels")
        )
        templated = set(
            NotificationTemplate.objects.using(alias)
            .filter(is_active=True, event_type__is_active=True)
            .values_list("event_type__key", "channel")
        )
    except (ProgrammingError, OperationalError):
        return []

    gaps = [
        (key, channel)
        for key, channels in declared
        for channel in (channels or [])
        if (key, channel) not in templated
    ]
    return sorted(gaps)


# Turn the raw pairs into one readable message rather than one message per row.
def _build_warning(gaps, alias, name_database=False):
    """
    Build the single Warning for one database.

    One message per missing pair would be dozens of near-identical lines on an
    unseeded database, so the counts lead and the event keys follow, grouped by
    key with their channels. Every affected key is named: a truncated list would
    leave somebody guessing at exactly the moment they need to act.
    """
    by_key = {}
    for key, channel in gaps:
        by_key.setdefault(key, []).append(channel)

    affected = ", ".join(
        f"{key} ({', '.join(channels)})" for key, channels in sorted(by_key.items())
    )
    scope = f"On database '{alias}': " if name_database else ""

    return CheckWarning(
        (
            f"{scope}{len(by_key)} active notification event type(s) have no "
            f"active template on {len(gaps)} of the channel(s) they declare. "
            f"Those channels send nothing: dispatch logs a warning and skips "
            f"them, so no message, no failure and no retry is produced. "
            f"Affected: {affected}."
        ),
        hint=(
            "Run: python manage.py seed_notification_templates (build.sh runs "
            "it on every deploy), or create the templates by hand. If an event "
            "type should no longer deliver on a channel, drop that channel from "
            "its supported_channels in vs_notifications/constants.py, or retire "
            "the event type with is_active=False."
        ),
        id=CHECK_ID,
    )
