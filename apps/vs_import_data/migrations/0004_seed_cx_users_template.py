"""Seed a ready-to-use system import template for the cx_users dataset type.

Mirrors how schools/branches templates are configured, but shipped so the CX
Users bulk-upload wizard has a template out of the box. Idempotent by template
code; reversible (removes the seeded template).
"""
from django.db import migrations


TEMPLATE_CODE = "cx_users_master"

# (column_name / header, target_field, data_type, required, sample, help, allowed_values)
COLUMNS = [
    ("First Name", "first_name", "string", True, "Ada", "The hire's first name.", []),
    ("Last Name", "last_name", "string", True, "Obi", "The hire's last name.", []),
    ("Email", "email", "email", True, "ada.obi@codexng.com", "Work email — must be unique.", []),
    ("Role Key", "role", "string", True, "xvs_platform_admin",
     "The role template key to assign (e.g. xvs_platform_admin).", []),
    ("Phone", "phone", "string", False, "+2348012345678", "Optional phone number.", []),
    ("Gender", "gender", "choice", False, "FEMALE", "MALE or FEMALE.", ["MALE", "FEMALE"]),
    ("Job Title", "job_title", "string", False, "Support Analyst", "Optional job title.", []),
    ("Employment Type", "employment_type", "choice", False, "FULL_TIME",
     "One of FULL_TIME, PART_TIME, CONTRACT, INTERN.",
     ["FULL_TIME", "PART_TIME", "CONTRACT", "INTERN"]),
    ("Position", "position", "string", False, "ENG-MGR",
     "Optional organogram seat — a Position id or code.", []),
    ("Date Joined", "date_joined", "date", False, "2026-07-20", "Optional YYYY-MM-DD.", []),
]


def seed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template, _ = ImportTemplate.objects.get_or_create(
        code=TEMPLATE_CODE,
        defaults={
            "name": "CX Users",
            "dataset_type": "cx_users",
            "status": "active",
            "default_file_format": "csv",
            "description": "Bulk-add CodeX platform staff. Each row is created and "
                           "submitted for approval, then appears in CX Users.",
            "instructions": "Fill one CX staff member per row. First name, last name, "
                            "email and role key are required; the rest are optional.",
            "allow_sample_row": True,
            "sample_row_data": {col[0]: col[4] for col in COLUMNS},
        },
    )
    for order, (name, target, dtype, required, sample, help_text, allowed) in enumerate(COLUMNS, start=1):
        ImportTemplateColumn.objects.get_or_create(
            template=template,
            target_field=target,
            defaults={
                "column_name": name,
                "data_type": dtype,
                "is_required": required,
                "column_order": order,
                "sample_value": sample,
                "help_text": help_text,
                "allowed_values": allowed,
            },
        )


def unseed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplate.objects.filter(code=TEMPLATE_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_import_data", "0003_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
