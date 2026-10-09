"""Install the three official templates used by organogram bulk upload."""

from django.db import migrations
from django.utils import timezone


TEMPLATES = [
    {
        "code": "cx_org_units_v1",
        "defaults": {
            "name": "CX Organogram Units Import",
            "dataset_type": "org_units",
            "status": "active",
            "default_file_format": "csv",
            "description": "Template for CodeX divisions, departments and teams.",
            "instructions": (
                "Import divisions first, then departments, then teams. Parent Code "
                "must name an earlier row or an existing org unit. Re-importing a "
                "code updates that unit."
            ),
            "allow_sample_row": True,
            "sample_row_data": {
                "Code": "DV-OPS", "Name": "Operations", "Kind": "DIVISION",
                "Parent Code": "", "Description": "Operations division", "Active": "Yes",
            },
            "is_download_enabled": True,
        },
        "columns": [
            ("Code", "code", "Code", "Stable unique org-unit code.", "string", True, True, 40, None, "DV-OPS", 1),
            ("Name", "name", "Name", "Org-unit name.", "string", True, False, 150, None, "Operations", 2),
            ("Kind", "kind", "Kind", "DIVISION, DEPARTMENT or TEAM.", "choice", True, False, None, ["DIVISION", "DEPARTMENT", "TEAM"], "DIVISION", 3),
            ("Parent Code", "parent_code", "Parent Code", "Blank for a division; required for lower tiers.", "string", False, False, 40, None, "", 4),
            ("Description", "description", "Description", "Optional description.", "string", False, False, None, None, "", 5),
            ("Active", "is_active", "Active", "Yes or No.", "boolean", False, False, None, None, "Yes", 6),
        ],
    },
    {
        "code": "cx_positions_v1",
        "defaults": {
            "name": "CX Organogram Positions Import",
            "dataset_type": "positions",
            "status": "active",
            "default_file_format": "csv",
            "description": "Template for CodeX organogram seats and solid reporting lines.",
            "instructions": (
                "Import org units first. Manager positions must appear before the "
                "positions that report to them. References use codes and role keys."
            ),
            "allow_sample_row": True,
            "sample_row_data": {
                "Code": "COO", "Title": "Chief Operating Officer",
                "Org Unit Code": "DV-OPS", "Reports To Code": "",
                "Default Role Key": "xvs_platform_admin", "Headcount": 1, "Active": "Yes",
            },
            "is_download_enabled": True,
        },
        "columns": [
            ("Code", "code", "Code", "Stable unique position code.", "string", True, True, 40, None, "COO", 1),
            ("Title", "title", "Title", "Position title.", "string", True, False, 150, None, "Chief Operating Officer", 2),
            ("Org Unit Code", "org_unit_code", "Org Unit Code", "Existing org-unit code.", "string", True, False, 40, None, "DV-OPS", 3),
            ("Reports To Code", "reports_to_code", "Reports To Code", "Blank for a top-level position; otherwise an earlier position code.", "string", False, False, 40, None, "", 4),
            ("Default Role Key", "default_role_key", "Default Role Key", "Optional role-template key in the platform tenant.", "string", False, False, 120, None, "", 5),
            ("Headcount", "headcount", "Headcount", "Positive whole number.", "integer", False, False, None, None, "1", 6),
            ("Active", "is_active", "Active", "Yes or No.", "boolean", False, False, None, None, "Yes", 7),
        ],
    },
    {
        "code": "cx_matrix_reports_v1",
        "defaults": {
            "name": "CX Matrix Reporting Lines Import",
            "dataset_type": "matrix_reports",
            "status": "active",
            "default_file_format": "csv",
            "description": "Template for CodeX dotted reporting lines.",
            "instructions": (
                "Import positions first. Each position and reports-to pair may "
                "appear once; re-importing that pair updates its label."
            ),
            "allow_sample_row": True,
            "sample_row_data": {
                "Position Code": "OPS-ANALYST", "Reports To Code": "CFO",
                "Relationship Label": "Finance oversight",
            },
            "is_download_enabled": True,
        },
        "columns": [
            ("Position Code", "position_code", "Position Code", "Existing position code.", "string", True, False, 40, None, "OPS-ANALYST", 1),
            ("Reports To Code", "reports_to_code", "Reports To Code", "Existing position code.", "string", True, False, 40, None, "CFO", 2),
            ("Relationship Label", "relationship_label", "Relationship Label", "Optional dotted-line label.", "string", False, False, 120, None, "Finance oversight", 3),
        ],
    },
]


def seed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    for definition in TEMPLATES:
        template, _ = ImportTemplate.objects.update_or_create(
            code=definition["code"],
            defaults={**definition["defaults"], "published_at": timezone.now()},
        )
        target_fields = {column[1] for column in definition["columns"]}
        ImportTemplateColumn.objects.filter(template=template).exclude(
            target_field__in=target_fields,
        ).delete()
        for (
            column_name, target_field, display_name, help_text, data_type,
            is_required, is_unique, max_length, allowed_values, sample_value,
            column_order,
        ) in definition["columns"]:
            ImportTemplateColumn.objects.update_or_create(
                template=template,
                target_field=target_field,
                defaults={
                    "column_name": column_name,
                    "display_name": display_name,
                    "help_text": help_text,
                    "data_type": data_type,
                    "is_required": is_required,
                    "is_unique": is_unique,
                    "max_length": max_length,
                    "allowed_values": allowed_values or [],
                    "sample_value": sample_value,
                    "default_value": "Yes" if target_field == "is_active" else ("1" if target_field == "headcount" else ""),
                    "column_order": column_order,
                },
            )


class Migration(migrations.Migration):
    dependencies = [("vs_import_data", "0024_alter_importbatch_dataset_type_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
