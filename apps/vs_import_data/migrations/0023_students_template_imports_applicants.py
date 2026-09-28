"""The students template says that a school requiring documents gets applicants.

A school can require documents before a child joins the roll (Settings,
Applicants). A spreadsheet carries no documents, so at such a school the
students import brings every row in as an applicant instead of enrolling and
placing it, and the template's opening paragraph, which says every student is
created on the roll, gains one sentence saying so.

The sentence is added only where the paragraph still reads as it was written,
so a template whose guidance has been edited since is left alone. Reversible:
the sentence is taken out again.
"""
from django.db import migrations

#: (as written, as it now reads), for the template's instructions.
INSTRUCTIONS = [
    (
        "A row with no class still imports and the student waits under "
        "Classes and transfers.",
        "A row with no class still imports and the student waits under "
        "Classes and transfers. At a school that needs documents before "
        "enrolling (Settings, Applicants), every row comes in as an applicant "
        "instead, listed under the year of the class named, and joins the "
        "roll once those documents are uploaded and the child is confirmed.",
    ),
]


def _swap(apps, forward):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    template = ImportTemplate.objects.filter(code="students_v1").first()
    if template is None:
        return
    text = template.instructions or ""
    for old, new in INSTRUCTIONS:
        a, b = (old, new) if forward else (new, old)
        if forward and new in text:
            continue
        text = text.replace(a, b)
    template.instructions = text
    template.save(update_fields=["instructions"])


def forwards(apps, schema_editor):
    _swap(apps, forward=True)


def backwards(apps, schema_editor):
    _swap(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0022_guardian_templates_follow_school_rules"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
