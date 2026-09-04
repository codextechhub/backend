"""Bind guardian photographs to the guardians they are of.

A guardian's photograph is added to the model after the module's first backfill
has already run, so it needs its own pass: ``0002`` resolves against a project
state in which the column does not exist yet, and a binding filed there would
either be skipped or fail outright.

Its binding is declared in
``core.migrations.0006_backfill_storedfile_bindings.LATER_BINDINGS`` under this
migration's key, and the pass itself is the shared one, so a null tenant is
treated here exactly as it is everywhere else: the row is left unbound, because
a refused file is the safe failure and a file bound to a guess is one served to
the wrong school.

Nothing can predate the column, so this is a no-op by construction. It runs
anyway, for the same reason ``0002`` does: a fixture or a seeded load can write
a file without passing through the save hook that binds it, and an unbound row
is not served at all.
"""
from django.db import migrations

#: The key this migration owns in ``LATER_BINDINGS``.
WAVE = "vs_students.0004_bind_guardian_photos"


def backfill(apps, schema_editor):
    import importlib

    # Imported by string: the module name starts with a digit.
    module = importlib.import_module(
        "core.migrations.0006_backfill_storedfile_bindings",
    )
    module.bind_rows(apps, module.LATER_BINDINGS[WAVE])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0003_guardian_photo"),
        ("core", "0006_backfill_storedfile_bindings"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
