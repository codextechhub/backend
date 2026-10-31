"""A child's blood group, allergies and conditions are stored encrypted.

The columns become :class:`core.fields.EncryptedTextField` and every value
already stored, here and in the record history, is encrypted in place.
Reversible: the values are written back as plain text first."""

from django.db import migrations

import core.fields
from core.encryption import decrypt_rows, encrypt_history, encrypt_rows

FIELDS = ("blood_group", "allergies", "conditions")


def forward(apps, schema_editor):
    encrypt_rows(apps.get_model("vs_students", "Student"), FIELDS)
    encrypt_history(apps.get_model("vs_history", "RecordVersion"), "vs_students.student", FIELDS)


def backward(apps, schema_editor):
    encrypt_history(
        apps.get_model("vs_history", "RecordVersion"), "vs_students.student", FIELDS, reverse=True,
    )
    decrypt_rows(apps.get_model("vs_students", "Student"), FIELDS)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0018_person_erased_at"),
        ("vs_history", "0002_trackingstart"),
    ]

    operations = [
        migrations.AlterField(
            model_name="student",
            name="blood_group",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=4),
        ),
        migrations.AlterField(
            model_name="student",
            name="allergies",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="student",
            name="conditions",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=200),
        ),
        migrations.RunPython(forward, backward),
    ]
