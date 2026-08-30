"""Bind this module's stored files to the records they belong to.

``core.binding`` stamps a file's owner when the record is saved, so every file
written through the API is bound from the first one. This migration exists for
the rows that could exist without ever passing through that path - a fixture, a
seeder, or a load run before the app config was wired - because "there cannot
be any rows yet" is exactly the assumption that turns out to be wrong, and an
unbound row is not served at all.

The bindings themselves are declared in
``core.migrations.0006_backfill_storedfile_bindings.LATER_BINDINGS``, so the
exhaustiveness test in ``core.tests`` reads one union and a FileField cannot be
added to this module without somebody deciding where it belongs.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    import importlib

    # Imported by string: the module name starts with a digit.
    module = importlib.import_module(
        "core.migrations.0006_backfill_storedfile_bindings",
    )
    ContentType = apps.get_model("contenttypes", "ContentType")
    StoredFile = apps.get_model("core", "StoredFile")

    for app_label, model_name, field_name, tenant_lookup in module.LATER_BINDINGS:
        if app_label != "vs_students":
            continue
        model = apps.get_model(app_label, model_name)
        content_type = ContentType.objects.get_for_model(model)
        rows = (
            model.objects
            .exclude(**{field_name: ""})
            .exclude(**{f"{field_name}__isnull": True})
            .values_list("pk", field_name, tenant_lookup)
            .iterator(chunk_size=1000)
        )
        for pk, name, tenant_id in rows:
            if not name or tenant_id is None:
                # Refused is the safe failure; bound to a guess is the unsafe
                # one. Same rule as the original backfill.
                continue
            StoredFile.objects.filter(name=name).update(
                tenant_id=tenant_id,
                owner_content_type=content_type,
                owner_object_id=str(pk),
                owner_field=field_name,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0001_initial"),
        ("core", "0006_backfill_storedfile_bindings"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
