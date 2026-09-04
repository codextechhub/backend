"""The guardians template.

Eleven columns, and one row is one LINK rather than one parent, because the
relationship and the primary-contact flag belong to the pair: Mrs Adeleke is
Mother to Amaka and Legal Guardian to the niece she also has at the school. A
parent with three children here appears three times.

The admission number is asked for first even though many schools leave it
blank, because it is the only exact way to say which child a row reaches. A
name and a date of birth are not an identifier - the student import already
warns that two real children share both - so an ambiguous match is refused
rather than resolved to whichever row came first.
"""
from django.db import migrations
from django.utils import timezone


TEMPLATE_DEFAULTS = {
    "name": "Guardians Import",
    "dataset_type": "guardians",
    "status": "active",
    "default_file_format": "xlsx",
    "description": (
        "Template for loading the people a school calls about each child: "
        "second parents, grandparents and legal guardians."
    ),
    "instructions": (
        "ONE ROW PER GUARDIAN AND CHILD. A parent with three children at this "
        "school gets three rows, because the relationship and the primary "
        "contact are about the pair and not about the person. "
        "\n\n"
        "SAY WHICH CHILD by admission number where you have one: it is the "
        "only exact answer. Otherwise give the child's first name, last name "
        "and date of birth, and be aware that two children at one school can "
        "share all three - where they do, the row is refused and asks for the "
        "admission number instead, rather than guessing. "
        "\n\n"
        "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND. A parent "
        "already at this school is reused, so their children are recorded as "
        "siblings of one household. Give the same contact for the same person "
        "and a different one for unrelated people, or two families are merged "
        "into one. "
        "\n\n"
        "WHAT IS REFUSED. A guardian name and a phone number on every row. A "
        "phone with too few digits to ring, or an email that is not an "
        "address. A child this school does not have, or one named so "
        "ambiguously that more than one matches. The same guardian and child "
        "twice in one file. Two rows both making somebody the primary contact "
        "for one child, because a child has one and those two rows disagree. "
        "\n\n"
        "WHAT IS WARNED ABOUT, AND STILL IMPORTED. A guardian already linked "
        "to that child, whose row is skipped rather than linking them twice. A "
        "contact another row or another guardian already uses under a "
        "different name: the child joins that household and the name in your "
        "row is not recorded. A row that makes somebody primary for a child "
        "who already has one, naming who loses it - the change is silent "
        "otherwise. A child this file gives contacts to and marks no primary "
        "for, which leaves the school with numbers and no answer to who it "
        "calls first. A relationship this school does not record, which is "
        "imported as Other. "
        "\n\n"
        "Primary Contact takes Yes or No. Occupation and Home Address are "
        "optional and are only used when the guardian is new to the school."
    ),
    "allow_sample_row": True,
    "sample_row_data": {
        "Guardian Name": "Mr. Emeka Adeleke",
        "Guardian Phone": "08035550102",
        "Guardian Email": "emeka.adeleke@example.com",
        "Admission Number": "BFS/2025/0142",
        "Student First Name": "",
        "Student Last Name": "",
        "Student Date of Birth": "",
        "Relationship": "Father",
        "Primary Contact": "No",
        "Occupation": "Engineer",
        "Home Address": "",
    },
    "validation_rules": {
        "min_rows": 1,
        # A school of two thousand children has perhaps three thousand links.
        "max_rows": 5_000,
        "allowed_file_formats": ["csv", "xlsx"],
    },
    "is_download_enabled": True,
}


COLUMNS = [
    {
        "column_name": "Guardian Name", "target_field": "guardian_full_name",
        "display_name": "Guardian Name",
        "help_text": "The person the school calls.",
        "data_type": "string", "is_required": True, "max_length": 150,
        "sample_value": "Mr. Emeka Adeleke", "column_order": 1,
    },
    {
        "column_name": "Guardian Phone", "target_field": "guardian_phone",
        "display_name": "Guardian Phone",
        "help_text": (
            "A number the school can reach. Guardians are matched on it after "
            "email, so two rows sharing a number are treated as one person."
        ),
        "data_type": "string", "is_required": True, "max_length": 32,
        "sample_value": "08035550102", "column_order": 2,
    },
    {
        "column_name": "Guardian Email", "target_field": "guardian_email",
        "display_name": "Guardian Email",
        "help_text": (
            "How the school recognises a parent it already holds, and the "
            "address any parent account is issued to."
        ),
        "data_type": "email", "is_required": False, "max_length": 254,
        "sample_value": "emeka.adeleke@example.com", "column_order": 3,
    },
    {
        "column_name": "Admission Number", "target_field": "student_number",
        "display_name": "Admission Number",
        "help_text": (
            "The child's number, and the only exact way to say which child "
            "this row reaches. Give it where you have one; leave it blank and "
            "fill in the three columns below instead."
        ),
        "data_type": "string", "is_required": False, "max_length": 32,
        "sample_value": "BFS/2025/0142", "column_order": 4,
    },
    {
        "column_name": "Student First Name",
        "target_field": "student_first_name",
        "display_name": "Student First Name",
        "help_text": "Only needed when there is no admission number.",
        "data_type": "string", "is_required": False, "max_length": 100,
        "sample_value": "", "column_order": 5,
    },
    {
        "column_name": "Student Last Name",
        "target_field": "student_last_name",
        "display_name": "Student Last Name",
        "help_text": "Only needed when there is no admission number.",
        "data_type": "string", "is_required": False, "max_length": 100,
        "sample_value": "", "column_order": 6,
    },
    {
        "column_name": "Student Date of Birth",
        "target_field": "student_date_of_birth",
        "display_name": "Student Date of Birth",
        "help_text": (
            "YYYY-MM-DD. Only needed when there is no admission number, and "
            "it is what tells two children of the same name apart."
        ),
        "data_type": "date", "is_required": False,
        "sample_value": "", "column_order": 7,
    },
    {
        "column_name": "Relationship", "target_field": "relationship",
        "display_name": "Relationship",
        "help_text": (
            "Mother, Father, Uncle, Aunt, Grandparent, Legal guardian, "
            "Sibling or Other. Anything else is imported as Other."
        ),
        "data_type": "string", "is_required": False, "max_length": 32,
        "sample_value": "Father", "column_order": 8,
    },
    {
        "column_name": "Primary Contact", "target_field": "is_primary",
        "display_name": "Primary Contact",
        "help_text": (
            "Yes for the one person the school calls first about this child. "
            "A child has exactly one, so marking a new one moves it from "
            "whoever holds it now."
        ),
        "data_type": "string", "is_required": False, "max_length": 8,
        "sample_value": "No", "column_order": 9,
    },
    {
        "column_name": "Occupation", "target_field": "occupation",
        "display_name": "Occupation",
        "help_text": (
            "Optional, and only used when this guardian is new to the school."
        ),
        "data_type": "string", "is_required": False, "max_length": 100,
        "sample_value": "Engineer", "column_order": 10,
    },
    {
        "column_name": "Home Address", "target_field": "address",
        "display_name": "Home Address",
        "help_text": (
            "Optional, and only used when this guardian is new to the school. "
            "A guardian's address is not always the child's."
        ),
        "data_type": "string", "is_required": False, "max_length": 500,
        "sample_value": "", "column_order": 11,
    },
]


def seed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template, _ = ImportTemplate.objects.update_or_create(
        code="guardians_v1",
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
        ("vs_import_data", "0017_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
