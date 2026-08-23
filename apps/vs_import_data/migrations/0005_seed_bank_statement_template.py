from django.db import migrations
from django.utils import timezone


TEMPLATE_DEFAULTS = {
    "name": "Bank Statement Import",
    "dataset_type": "bank_statements",
    "status": "active",
    "default_file_format": "xlsx",
    "description": (
        "Template for importing one chronological bank-statement period "
        "into the finance reconciliation workbench."
    ),
    "instructions": (
        "Keep the rows oldest to newest. Enter a positive value in exactly one of "
        "Money In or Money Out on every row. Amounts are entered in major currency "
        "units (for example 1250.50, not kobo). Transaction ID should be the bank's "
        "stable unique reference when available. Balance is optional, but if used it "
        "must be filled on every row. The wizard verifies duplicate transactions, "
        "every running balance, and opening balance plus net movement equals closing "
        "balance before publish. The included sample assumes an opening balance of "
        "10000.00 and a closing balance of 11250.00; replace it with the bank's "
        "actual rows."
    ),
    "allow_sample_row": True,
    "sample_row_data": {
        "Transaction Date": "2026-07-01",
        "Description": "Customer payment",
        "Reference": "BANK-REF-0001",
        "Money In": "1250.00",
        "Money Out": "",
        "Transaction ID": "TXN-000001",
        "Balance": "11250.00",
    },
    "validation_rules": {
        "min_rows": 1,
        "max_rows": 50_000,
        "allowed_file_formats": ["csv", "xlsx"],
    },
    "is_download_enabled": True,
}


COLUMNS = [
    {
        "column_name": "Transaction Date",
        "target_field": "txn_date",
        "display_name": "Transaction Date",
        "help_text": "Bank transaction/value date. Rows must be oldest to newest.",
        "data_type": "date",
        "is_required": True,
        "sample_value": "2026-07-01",
        "column_order": 1,
    },
    {
        "column_name": "Description",
        "target_field": "description",
        "display_name": "Description",
        "help_text": "Narration shown on the bank statement.",
        "data_type": "string",
        "is_required": False,
        "max_length": 255,
        "sample_value": "POS settlement",
        "column_order": 2,
    },
    {
        "column_name": "Reference",
        "target_field": "reference",
        "display_name": "Reference",
        "help_text": "Human-readable bank or payment reference.",
        "data_type": "string",
        "is_required": False,
        "max_length": 64,
        "sample_value": "GTB-000123",
        "column_order": 3,
    },
    {
        "column_name": "Money In",
        "target_field": "money_in",
        "display_name": "Money In",
        "help_text": "Positive inflow in major currency units; leave blank for outflows.",
        "data_type": "string",
        "is_required": False,
        "max_length": 32,
        "sample_value": "150000.00",
        "column_order": 4,
    },
    {
        "column_name": "Money Out",
        "target_field": "money_out",
        "display_name": "Money Out",
        "help_text": "Positive outflow in major currency units; leave blank for inflows.",
        "data_type": "string",
        "is_required": False,
        "max_length": 32,
        "sample_value": "2500.00",
        "column_order": 5,
    },
    {
        "column_name": "Transaction ID",
        "target_field": "external_id",
        "display_name": "Transaction ID",
        "help_text": "Stable unique id supplied by the bank, when available.",
        "data_type": "string",
        "is_required": False,
        "is_unique": True,
        "max_length": 128,
        "sample_value": "918273645",
        "column_order": 6,
    },
    {
        "column_name": "Balance",
        "target_field": "balance",
        "display_name": "Balance",
        "help_text": "Optional running balance after this transaction.",
        "data_type": "string",
        "is_required": False,
        "max_length": 32,
        "sample_value": "2500000.00",
        "column_order": 7,
    },
]


def seed_bank_statement_template(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model(
        "vs_import_data",
        "ImportTemplateColumn",
    )

    template, _ = ImportTemplate.objects.update_or_create(
        code="bank_statements_v1",
        defaults={**TEMPLATE_DEFAULTS, "published_at": timezone.now()},
    )

    target_fields = {column["target_field"] for column in COLUMNS}
    ImportTemplateColumn.objects.filter(template=template).exclude(
        target_field__in=target_fields
    ).delete()

    for column in COLUMNS:
        ImportTemplateColumn.objects.update_or_create(
            template=template,
            target_field=column["target_field"],
            defaults={
                key: value
                for key, value in column.items()
                if key != "target_field"
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0004_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(
            seed_bank_statement_template,
            migrations.RunPython.noop,
        ),
    ]
