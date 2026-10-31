"""A vendor's bank account number is stored encrypted."""

from django.db import migrations

import core.fields
from core.encryption import decrypt_rows, encrypt_rows

FIELDS = ("bank_account_number",)


def forward(apps, schema_editor):
    encrypt_rows(apps.get_model("vs_procurement", "Vendor"), FIELDS)


def backward(apps, schema_editor):
    decrypt_rows(apps.get_model("vs_procurement", "Vendor"), FIELDS)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0049_kept_documents_refuse_delete"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendor",
            name="bank_account_number",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=32),
        ),
        migrations.RunPython(forward, backward),
    ]
