"""The account number a payout is sent to is stored encrypted."""

from django.db import migrations

import core.fields
from core.encryption import decrypt_rows, encrypt_rows

FIELDS = ("beneficiary_account_number",)


def forward(apps, schema_editor):
    encrypt_rows(apps.get_model("vs_payments", "PayoutInstruction"), FIELDS)


def backward(apps, schema_editor):
    decrypt_rows(apps.get_model("vs_payments", "PayoutInstruction"), FIELDS)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_payments", "0020_virtual_account_not_issued_event"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payoutinstruction",
            name="beneficiary_account_number",
            field=core.fields.EncryptedTextField(max_length=20),
        ),
        migrations.RunPython(forward, backward),
    ]
