"""Bind this module's stored files to the records they belong to.

``core.binding`` stamps a file's owner when the record is saved, so every file
written through the API is bound from the first one. This migration exists for
the rows that could exist without ever passing through that path - a fixture, a
seeder, or a load run before the app config was wired - because "there cannot
be any rows yet" is exactly the assumption that turns out to be wrong, and an
unbound row is not served at all.

The bindings themselves are declared in
``core.migrations.0006_backfill_storedfile_bindings.LATER_BINDINGS``, under this
migration's own key. Reading one key rather than the whole app's worth is what
keeps a field added by a later migration out of this one's project state, which
has never heard of it. The exhaustiveness test in ``core.tests`` reads the
union, so a FileField still cannot be added to this module without somebody
deciding which migration backfills it.
"""
from django.db import migrations

#: The key this migration owns in ``LATER_BINDINGS``.
WAVE = "vs_students.0002_bind_student_files"


def backfill(apps, schema_editor):
    import importlib

    # Imported by string: the module name starts with a digit.
    module = importlib.import_module(
        "core.migrations.0006_backfill_storedfile_bindings",
    )
    module.bind_rows(apps, module.LATER_BINDINGS[WAVE])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0001_initial"),
        ("core", "0006_backfill_storedfile_bindings"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
