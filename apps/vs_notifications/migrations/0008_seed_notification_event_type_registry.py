"""Put the notification event registry into every database that exists.

``NotificationEventType`` rows are reference data: dispatch resolves an event key
against this table, so a database with an empty registry cannot send anything.
Until now the only thing that created the rows was the
``seed_notification_event_types`` management command, which ``build.sh`` runs on
deploy. Every database that ``build.sh`` never touched - every test database, and
every fresh developer checkout before the command is run - therefore started with
an empty registry, and the two existing migrations that install event types
(0004 here, ``vs_procurement`` 0020) between them cover only 7 of the 47 keys.

What that cost in the test suite is why this migration exists.
``finalize_invitation`` catches and logs a dispatch failure, so a test that
created a user passed while the invitation path never actually ran, printing an
error-level traceback on the way through. The workaround was for individual test
classes to seed the registry themselves, which is a rule every new test class has
to remember and the reason several of them had grown a copy of it. A migration is
the shared boundary: the rows arrive with the database, and nobody has to
remember anything.

The command remains the way to resync a running install after the registry
changes. This migration is what guarantees the floor.

Deliberately not reversible in the destructive sense - see
``keep_event_types_on_reverse``.
"""
from django.db import migrations

# Imported, not snapshotted. Two reasons, and one non-reason.
#
# These are upserted reference data keyed on ``key``, and
# ``seed_notification_event_types`` already replays today's registry over
# whatever is in the database on every deploy. Importing keeps this migration and
# that command saying the same thing. A 47-entry inline snapshot would be a
# second authoritative copy of the catalogue, and it goes stale the moment
# somebody adds an event - while constants.py tells them, correctly, that adding
# an event means editing one list. A reviewer of that change has no reason to go
# looking inside a migrations directory for the other copy.
#
# The cost, taken knowingly: replaying this migration against an old database
# seeds today's registry rather than the one that existed when it was written.
# For idempotent, key-addressed reference data that is the desirable direction.
# The alternative is deliberately installing a catalogue we already know is out
# of date and waiting for the deploy-time command to correct it.
#
# The non-reason: "a migration must not import live code" is about the MODEL
# moving on, not about the payload. That hazard is handled below - rows go
# through ``apps.get_model`` and are built from an explicit field list, so a
# registry entry that grows a new key in future cannot hand this migration a
# column the historical model does not have.
from vs_notifications.constants import EVENT_TYPE_REGISTRY


# The NotificationEventType columns this migration knows how to write. Pinning
# the field set (rather than the data) is what keeps the migration replayable:
# extra keys added to a registry entry later are ignored here instead of raising.
REQUIRED_FIELDS = ("label", "source_module", "supported_channels")

# Optional registry keys and the fallbacks seed_event_types() applies, kept
# identical to it so the migration and the command cannot disagree about a row.
OPTIONAL_FIELD_DEFAULTS = {
    "description": "",
    "default_enabled": True,
    "is_transactional": False,
    "is_active": True,
}


def seed_event_type_registry(apps, schema_editor):
    """Upsert every registry entry, with the same semantics as seed_event_types()."""
    NotificationEventType = apps.get_model("vs_notifications", "NotificationEventType")

    for entry in EVENT_TYPE_REGISTRY:
        defaults = {field: entry[field] for field in REQUIRED_FIELDS}
        for field, fallback in OPTIONAL_FIELD_DEFAULTS.items():
            defaults[field] = entry.get(field, fallback)

        # update_or_create on the unique ``key``: re-running neither duplicates a
        # row nor leaves stale metadata behind. A key that has since left the
        # registry is left alone rather than deleted - retiring an event type
        # means is_active=False, never a delete, because the rows are referenced.
        NotificationEventType.objects.update_or_create(
            key=entry["key"],
            defaults=defaults,
        )


def keep_event_types_on_reverse(apps, schema_editor):
    """Intentionally does nothing. Deleting these rows would destroy real data.

    Three things point at NotificationEventType, and a reverse that removed the
    catalogue would hit all three:

    * ``Notification.event_type`` is PROTECT, and every notification ever sent
      holds one. The delete would raise ProtectedError part-way through the
      rollback, leaving the operator with a half-reversed migration.
    * ``NotificationTemplate.event_type`` is PROTECT for the same reason.
    * ``NotificationSetting.event_type`` is **CASCADE**. That one does not raise;
      it silently takes every platform default and every tenant's per-channel
      toggle down with it. An admin's deliberate choice to mute an event is not
      something a rollback may quietly discard.

    Reference data outliving the migration that introduced it is the correct
    outcome here. The rows are key-addressed and idempotent, they already exist
    on every deployed database, and ``seed_notification_event_types`` re-installs
    them on the next deploy regardless. There is nothing to undo.

    This is a named function rather than ``migrations.RunPython.noop`` because
    noop tells Django the same thing and the next reader nothing at all. The
    reason deleting is wrong needs to survive next to the code, exactly as 0004
    already does when it deactivates instead of deleting on reverse.
    """
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("vs_notifications", "0007_neutral_billing_document_copy"),
    ]

    operations = [
        migrations.RunPython(
            seed_event_type_registry,
            keep_event_types_on_reverse,
        ),
    ]
