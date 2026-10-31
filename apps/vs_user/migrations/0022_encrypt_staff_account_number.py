"""A CodeX staff member's bank account number is stored encrypted.

The column becomes :class:`core.fields.EncryptedTextField` and every value
already stored, here and in the record history, is encrypted in place."""

from django.db import migrations

import core.fields
from core.encryption import decrypt_rows, encrypt_history, encrypt_rows

FIELDS = ("account_number",)
RECORD_TYPE = "vs_user.platformstaffprofile"


def forward(apps, schema_editor):
    encrypt_rows(apps.get_model("vs_user", "PlatformStaffProfile"), FIELDS)
    encrypt_history(apps.get_model("vs_history", "RecordVersion"), RECORD_TYPE, FIELDS)


def backward(apps, schema_editor):
    encrypt_history(apps.get_model("vs_history", "RecordVersion"), RECORD_TYPE, FIELDS, reverse=True)
    decrypt_rows(apps.get_model("vs_user", "PlatformStaffProfile"), FIELDS)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_user", "0021_person_erased_at"),
        ("vs_history", "0002_trackingstart"),
    ]

    operations = [
        migrations.AlterField(
            model_name="platformstaffprofile",
            name="account_number",
            field=core.fields.EncryptedTextField(blank=True, default='', max_length=20),
        ),
        migrations.RunPython(forward, backward),
    ]
