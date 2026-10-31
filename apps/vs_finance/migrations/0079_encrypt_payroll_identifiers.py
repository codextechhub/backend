"""Staff tax IDs, pension PINs and pay bank account numbers are stored encrypted.

On the salary record, on each payroll line's copy of them, and on the staff
pay bank account."""

from django.db import migrations

import core.fields
from core.encryption import decrypt_rows, encrypt_rows

TABLES = (
    ("EmployeeSalary", ("tax_id", "pension_pin")),
    ("PayrollLine", ("tax_id", "pension_pin")),
    ("StaffBankAccount", ("account_number",)),
)


def forward(apps, schema_editor):
    for model, fields in TABLES:
        encrypt_rows(apps.get_model("vs_finance", model), fields)


def backward(apps, schema_editor):
    for model, fields in TABLES:
        decrypt_rows(apps.get_model("vs_finance", model), fields)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_finance", "0078_kept_documents_refuse_delete"),
    ]

    operations = [
        migrations.AlterField(
            model_name="employeesalary",
            name="tax_id",
            field=core.fields.EncryptedTextField(blank=True, default="", help_text="Tax identification number.", max_length=32),
        ),
        migrations.AlterField(
            model_name="employeesalary",
            name="pension_pin",
            field=core.fields.EncryptedTextField(blank=True, default="", help_text="Retirement savings account PIN.", max_length=32),
        ),
        migrations.AlterField(
            model_name="payrollline",
            name="tax_id",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="payrollline",
            name="pension_pin",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="staffbankaccount",
            name="account_number",
            field=core.fields.EncryptedTextField(blank=True, default="", max_length=20),
        ),
        migrations.RunPython(forward, backward),
    ]
