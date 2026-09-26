"""A guardian's name in parts, split from the one-line name every row carries.

Each existing guardian is split with :func:`schools.vs_students.names.split_full_name`
and flagged ``name_needs_review``. ``full_name`` is left exactly as it was typed:
until a person confirms the parts, it is the only reading of the name nobody
guessed, and it is what every list keeps showing in the meantime.

Reversing drops the parts and the flag; ``full_name`` was never changed, so
nothing is lost.
"""
from django.db import migrations, models


def split_existing(apps, schema_editor):
    from schools.vs_students.names import split_full_name

    Guardian = apps.get_model("vs_students", "Guardian")
    rows = []
    for guardian in Guardian.objects.all().only("pk", "full_name").iterator(chunk_size=1000):
        first, middle, last = split_full_name(guardian.full_name)
        guardian.first_name, guardian.middle_name, guardian.last_name = first, middle, last
        guardian.name_needs_review = True
        rows.append(guardian)
    Guardian.objects.bulk_update(
        rows, ["first_name", "middle_name", "last_name", "name_needs_review"],
        batch_size=1000,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0004_bind_guardian_photos"),
    ]

    operations = [
        migrations.AddField(
            model_name="guardian",
            name="first_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="guardian",
            name="middle_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="guardian",
            name="last_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="guardian",
            name="name_needs_review",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(split_existing, migrations.RunPython.noop),
    ]
