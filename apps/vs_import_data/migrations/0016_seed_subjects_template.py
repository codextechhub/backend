"""The subject template.

Six columns, and one row is one SUBJECT with the year groups it is taught at
listed beside it. A school holds its subjects that way - "Mathematics, JSS1 to
SSS3" - so the offerings are a column rather than a file of their own. A
separate offerings file would be two hundred rows of two names and nobody would
fill it in.

Semicolons separate the year groups, as the calendar template separates its
audience. A comma cannot: "Primary 4, 5 and 6" is a thing a school writes, and
splitting on commas would invent two year groups called "5" and "6".
"""
from django.db import migrations
from django.utils import timezone


TEMPLATE_DEFAULTS = {
    "name": "Subjects Import",
    "dataset_type": "subjects",
    "status": "active",
    "default_file_format": "xlsx",
    "description": (
        "Template for loading a school's subject list, and the year groups "
        "each subject is taught at."
    ),
    "instructions": (
        "ONE ROW PER SUBJECT. List every year group it is taught at in the "
        "Year Groups column, separated by semicolons: 'JSS1; JSS2; JSS3'. A "
        "subject exists once for the whole school, so do not give it a row per "
        "year group. "
        "\n\n"
        "WHAT IS REFUSED. A subject name on every row. A year group this "
        "school does not run in the year it is running now, named exactly as "
        "it appears in Academic Structure: a name that matches nothing is "
        "refused rather than skipped, because a subject quietly taught in one "
        "fewer year than you meant is not something any screen would show you. "
        "The same subject on two rows. A branch this school does not have. "
        "\n\n"
        "WHAT IS WARNED ABOUT, AND STILL IMPORTED. A subject already in your "
        "list: the year groups on that row are added to it rather than "
        "creating a second subject, so a corrected file can be re-uploaded "
        "safely. A subject naming no year group at all, which will exist in "
        "the catalogue and be taught nowhere. A year group that this file "
        "would leave with NO subject at all, which is an empty timetable for "
        "everybody in it. The same year group listed twice on one row. "
        "\n\n"
        "Core or Elective decides whether every pupil in the year group takes "
        "it. Anything the importer does not recognise is treated as Core. "
        "Department and Branch are optional; leave Branch blank for a subject "
        "the whole school teaches, which is the usual case. Build your year "
        "groups with the Academic Structure import first, or this file has "
        "nothing to attach to."
    ),
    "allow_sample_row": True,
    "sample_row_data": {
        "Subject": "Mathematics",
        "Year Groups": "JSS1; JSS2; JSS3",
        "Core or Elective": "Core",
        "Department": "Sciences",
        "Branch": "",
        "Description": "",
    },
    "validation_rules": {
        "min_rows": 1,
        # A large school teaches sixty or so. Five hundred is far past anything
        # real and still cheap to validate.
        "max_rows": 500,
        "allowed_file_formats": ["csv", "xlsx"],
    },
    "is_download_enabled": True,
}


COLUMNS = [
    {
        "column_name": "Subject", "target_field": "subject",
        "display_name": "Subject",
        "help_text": (
            "What the subject is called. It exists once for the whole school, "
            "so give it one row however many year groups take it."
        ),
        "data_type": "string", "is_required": True, "max_length": 100,
        "sample_value": "Mathematics", "column_order": 1,
    },
    {
        "column_name": "Year Groups", "target_field": "levels",
        "display_name": "Year Groups",
        "help_text": (
            "Where it is taught, separated by semicolons: 'JSS1; JSS2; JSS3'. "
            "Each name must match a year group this school runs this year. "
            "Leave blank for a subject that is on the list but taught nowhere."
        ),
        "data_type": "string", "is_required": False, "max_length": 500,
        "sample_value": "JSS1; JSS2; JSS3", "column_order": 2,
    },
    {
        "column_name": "Core or Elective", "target_field": "kind",
        "display_name": "Core or Elective",
        "help_text": (
            "Core means every pupil in the year group takes it. Elective means "
            "they choose. Blank is treated as Core."
        ),
        "data_type": "string", "is_required": False, "max_length": 20,
        "sample_value": "Core", "column_order": 3,
    },
    {
        "column_name": "Department", "target_field": "department",
        "display_name": "Department",
        "help_text": (
            "Optional faculty grouping: Sciences, Arts, Languages. Created the "
            "first time it appears."
        ),
        "data_type": "string", "is_required": False, "max_length": 100,
        "sample_value": "Sciences", "column_order": 4,
    },
    {
        "column_name": "Branch", "target_field": "branch",
        "display_name": "Branch",
        "help_text": (
            "The branch that teaches it, written exactly as it appears in "
            "Branches. Leave blank for a subject the whole school teaches, "
            "which is the usual case."
        ),
        "data_type": "string", "is_required": False, "max_length": 120,
        "sample_value": "", "column_order": 5,
    },
    {
        "column_name": "Description", "target_field": "description",
        "display_name": "Description",
        "help_text": "Optional. Anything the school wants shown with it.",
        "data_type": "string", "is_required": False, "max_length": 500,
        "sample_value": "", "column_order": 6,
    },
]


def seed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template, _ = ImportTemplate.objects.update_or_create(
        code="subjects_v1",
        defaults={**TEMPLATE_DEFAULTS, "published_at": timezone.now()},
    )

    target_fields = {column["target_field"] for column in COLUMNS}
    ImportTemplateColumn.objects.filter(template=template).exclude(
        target_field__in=target_fields,
    ).delete()

    for column in COLUMNS:
        ImportTemplateColumn.objects.update_or_create(
            template=template,
            target_field=column["target_field"],
            defaults={
                key: value for key, value in column.items()
                if key != "target_field"
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0015_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
