"""Guardian names in parts, on the students and the guardians templates.

A guardian's name is stored as first, middle and last name, and a name that
arrives on one line is split by a rule and marked for somebody at the school
to check, because no rule can tell which word of "Adaeze Okafor Bello" is the
surname. Both templates carried only the one-line Guardian Name, so every
guardian a school imported arrived marked "check name".

This adds Guardian First Name, Guardian Middle Name and Guardian Last Name in
front of Guardian Name, which stays as an optional fallback for a school that
has only one line. The rules live in ``schools.vs_students.imports``
(``read_guardian_name``); this writes the columns and the guidance.
Columns are keyed on ``target_field``, as ``0011`` and ``0018`` key them.
"""
from django.db import migrations


PART_COLUMNS = [
    {
        "column_name": "Guardian First Name", "target_field": "guardian_first_name",
        "display_name": "Guardian First Name",
        "help_text": (
            "The guardian's first name. With the last name, this is how the "
            "school has the name exactly as the family spells it."
        ),
        "data_type": "string", "is_required": False, "max_length": 100,
    },
    {
        "column_name": "Guardian Middle Name", "target_field": "guardian_middle_name",
        "display_name": "Guardian Middle Name",
        "help_text": "Optional.",
        "data_type": "string", "is_required": False, "max_length": 100,
    },
    {
        "column_name": "Guardian Last Name", "target_field": "guardian_last_name",
        "display_name": "Guardian Last Name",
        "help_text": "The guardian's surname. Needed whenever a first name is given.",
        "data_type": "string", "is_required": False, "max_length": 100,
    },
]

ONE_LINE_HELP = (
    "Only for a file without the three name columns: the name on one line. It "
    "still imports, but it is split by a rule and marked for somebody at the "
    "school to check, because no rule can tell which word is the surname."
)

GUIDANCE = (
    "\n\n"
    "GUARDIAN NAMES. Fill Guardian First Name and Guardian Last Name, and "
    "Guardian Middle Name where there is one. A row that fills only Guardian "
    "Name still imports, with a warning, and that guardian is marked for "
    "somebody at the school to check the name, because no rule can tell which "
    "word of a one-line name is the surname."
)

SAMPLES = {
    "students_v1": ("Chukwudi", "", "Nwosu"),
    "guardians_v1": ("Emeka", "", "Adeleke"),
}


def add_parts(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    for code, (first, middle, last) in SAMPLES.items():
        template = ImportTemplate.objects.filter(code=code).first()
        if template is None:
            continue
        one_line = ImportTemplateColumn.objects.filter(
            template=template, target_field="guardian_full_name",
        ).first()
        if one_line is None:
            continue
        existing = set(
            ImportTemplateColumn.objects.filter(template=template)
            .values_list("target_field", flat=True)
        )
        if "guardian_first_name" not in existing:
            start = one_line.column_order
            for column in ImportTemplateColumn.objects.filter(
                template=template, column_order__gte=start,
            ).order_by("-column_order"):
                column.column_order += len(PART_COLUMNS)
                column.save(update_fields=["column_order"])
            samples = (first, middle, last)
            for offset, column in enumerate(PART_COLUMNS):
                ImportTemplateColumn.objects.create(
                    template=template,
                    column_order=start + offset,
                    sample_value=samples[offset],
                    **column,
                )
        one_line.is_required = False
        one_line.help_text = ONE_LINE_HELP
        one_line.sample_value = ""
        one_line.save(update_fields=["is_required", "help_text", "sample_value"])

        sample = dict(template.sample_row_data or {})
        sample.update({
            "Guardian First Name": first,
            "Guardian Middle Name": middle,
            "Guardian Last Name": last,
            "Guardian Name": "",
        })
        template.sample_row_data = sample
        if "GUARDIAN NAMES." not in (template.instructions or ""):
            template.instructions = (template.instructions or "") + GUIDANCE
        template.save(update_fields=["sample_row_data", "instructions"])


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0019_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(add_parts, migrations.RunPython.noop),
    ]
