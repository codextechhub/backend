"""The student import template.

Fifteen columns. The design's own template screen lists twelve and omits
``branch`` and ``guardian_email``; both are carried here, because the design
also has a branch switcher and so is not a single-branch product, and because
the guardian matching rule matches on an email it would otherwise never
receive - which would split every family whose children arrive in one file.

``admission_date`` is the fifteenth. A school importing its history knows when
each child joined, and without the column every imported student is stamped
with the date of the import.
"""
from django.db import migrations
from django.utils import timezone


TEMPLATE_DEFAULTS = {
    "name": "Student Import",
    "dataset_type": "students",
    "status": "active",
    "default_file_format": "xlsx",
    "description": (
        "Template for loading a school's existing students, with one guardian "
        "each, onto the roll."
    ),
    "instructions": (
        "One row per child. Classes must already exist in Academic Structure "
        "for the active session - a class name the engine cannot find is a "
        "hard error, not a warning. A row with no class still imports, and the "
        "student appears under Classes and transfers waiting to be placed. "
        "Admission numbers must be unique across the whole school, not just "
        "this file; leave the column blank on any number of rows unless your "
        "school has set a rule requiring one. Guardians are matched on email "
        "first and phone second, so three siblings naming the same guardian "
        "create one guardian and three links. Branch is required if your "
        "school has more than one; leave it blank if it has one. Imported "
        "students arrive Enrolled."
    ),
    "allow_sample_row": True,
    "sample_row_data": {
        "First Name": "Chiamaka",
        "Middle Name": "Adaeze",
        "Last Name": "Nwosu",
        "Date of Birth": "2013-04-18",
        "Gender": "Female",
        "Admission No.": "",
        "Admission Date": "2025-09-08",
        "Branch": "",
        "Class": "JSS1 A",
        "Guardian Name": "Mr. Chukwudi Nwosu",
        "Guardian Phone": "08035550101",
        "Guardian Email": "chukwudi.nwosu@example.ng",
        "Guardian Relationship": "Father",
        "Home Address": "14 Admiralty Way, Lekki Phase 1, Lagos",
        "Previous School": "",
    },
    "validation_rules": {
        "min_rows": 1,
        "max_rows": 5_000,
        "allowed_file_formats": ["csv", "xlsx"],
    },
    "is_download_enabled": True,
}


COLUMNS = [
    {
        "column_name": "First Name", "target_field": "first_name",
        "display_name": "First Name",
        "help_text": "The child's first name.",
        "data_type": "string", "is_required": True,
        "sample_value": "Chiamaka", "column_order": 1,
    },
    {
        "column_name": "Middle Name", "target_field": "middle_name",
        "display_name": "Middle Name",
        "help_text": "Optional.",
        "data_type": "string", "is_required": False,
        "sample_value": "Adaeze", "column_order": 2,
    },
    {
        "column_name": "Last Name", "target_field": "last_name",
        "display_name": "Last Name",
        "help_text": "The child's surname.",
        "data_type": "string", "is_required": True,
        "sample_value": "Nwosu", "column_order": 3,
    },
    {
        "column_name": "Date of Birth", "target_field": "date_of_birth",
        "display_name": "Date of Birth",
        "help_text": "YYYY-MM-DD. A day-first date such as 12/03/2014 is read as 12 March.",
        "data_type": "date", "is_required": True,
        "sample_value": "2013-04-18", "column_order": 4,
    },
    {
        "column_name": "Gender", "target_field": "gender",
        "display_name": "Gender",
        "help_text": "Female or Male.",
        "data_type": "choice", "is_required": True,
        "allowed_values": ["Female", "Male"],
        "sample_value": "Female", "column_order": 5,
    },
    {
        "column_name": "Admission No.", "target_field": "student_number",
        "display_name": "Admission No.",
        "help_text": (
            "Your school's own number. Optional unless your school has set a "
            "rule requiring one. Must be unique within your school."
        ),
        "data_type": "string", "is_required": False,
        "sample_value": "", "column_order": 6,
    },
    {
        "column_name": "Admission Date", "target_field": "admission_date",
        "display_name": "Admission Date",
        "help_text": "YYYY-MM-DD. Defaults to today if left blank.",
        "data_type": "date", "is_required": False,
        "sample_value": "2025-09-08", "column_order": 7,
    },
    {
        "column_name": "Branch", "target_field": "branch",
        "display_name": "Branch",
        "help_text": (
            "Required if your school has more than one branch. Leave blank if "
            "it has one."
        ),
        "data_type": "string", "is_required": False,
        "sample_value": "", "column_order": 8,
    },
    {
        "column_name": "Class", "target_field": "class",
        "display_name": "Class",
        "help_text": (
            "Must already exist in Academic Structure for the active session. "
            "Leave blank to enrol the student without a class."
        ),
        "data_type": "string", "is_required": False,
        "sample_value": "JSS1 A", "column_order": 9,
    },
    {
        "column_name": "Guardian Name", "target_field": "guardian_full_name",
        "display_name": "Guardian Name",
        "help_text": "The parent or guardian this child is reached through.",
        "data_type": "string", "is_required": True,
        "sample_value": "Mr. Chukwudi Nwosu", "column_order": 10,
    },
    {
        "column_name": "Guardian Phone", "target_field": "guardian_phone",
        "display_name": "Guardian Phone",
        "help_text": "A number the school can reach them on.",
        "data_type": "string", "is_required": True,
        "sample_value": "08035550101", "column_order": 11,
    },
    {
        "column_name": "Guardian Email", "target_field": "guardian_email",
        "display_name": "Guardian Email",
        "help_text": (
            "Optional, and how siblings are joined: rows naming the same "
            "address create one guardian rather than one each."
        ),
        "data_type": "string", "is_required": False,
        "sample_value": "chukwudi.nwosu@example.ng", "column_order": 12,
    },
    {
        "column_name": "Guardian Relationship",
        "target_field": "guardian_relationship",
        "display_name": "Guardian Relationship",
        "help_text": (
            "Father, Mother, Uncle, Aunt, Grandparent, Legal guardian, "
            "Sibling or Other. Anything else is imported as Other."
        ),
        "data_type": "string", "is_required": False,
        "sample_value": "Father", "column_order": 13,
    },
    {
        "column_name": "Home Address", "target_field": "address",
        "display_name": "Home Address",
        "help_text": "Optional.",
        "data_type": "string", "is_required": False,
        "sample_value": "14 Admiralty Way, Lekki Phase 1, Lagos",
        "column_order": 14,
    },
    {
        "column_name": "Previous School", "target_field": "previous_school",
        "display_name": "Previous School",
        "help_text": "Optional.",
        "data_type": "string", "is_required": False,
        "sample_value": "", "column_order": 15,
    },
]


def seed_students_template(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template, _ = ImportTemplate.objects.update_or_create(
        code="students_v1",
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
        ("vs_import_data", "0010_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(
            seed_students_template,
            migrations.RunPython.noop,
        ),
    ]
