"""The students template's guidance follows each school's own enrolment rules.

The guidance was written when every school had the same rules: an age range
of 2 to 25, no required details beyond the fixed ones, over-capacity rows
warned about, and admission numbers typed by hand. A school now sets each of
those in Settings, Enrolment and Admission numbers, and the template is one
row shared by every school, so it can no longer state one school's numbers.

Each sentence is swapped only where it still reads as it was written, so a
template whose guidance has been edited since is left alone. Reversible: the
sentences are swapped back.
"""
from django.db import migrations

#: (as written, as it now reads), for the template's instructions.
INSTRUCTIONS = [
    (
        "A date of birth that is in the future, or whose year would make the "
        "child under 2 or over 25, because that is almost always a mistyped "
        "year.",
        "A date of birth that is in the future, or whose year puts the child "
        "outside your school's age range (2 to 25 unless your school has set "
        "its own, in Settings, Enrolment), because that is almost always a "
        "mistyped year. A detail your school requires at enrolment, where "
        "this file has a column for it. A row past a class's last seat, where "
        "your school never puts classes over capacity.",
    ),
    (
        "An admission number that breaks your school's format, that another "
        "student already holds, or that appears twice in this file.",
        "An admission number that breaks the format of the child's branch, "
        "that another student already holds, or that appears twice in this "
        "file. Where that branch issues numbers automatically, a blank one is "
        "given the next number instead.",
    ),
    (
        "A class that this file fills past its capacity, naming the class and "
        "the count.",
        "A class that this file fills past its capacity, naming the class and "
        "the count, where your school allows it.",
    ),
]

#: target_field -> [(as written, as it now reads)] for column help.
HELP = {
    "date_of_birth": [(
        "A year that would make the child under 2 or over 25 is refused, "
        "because it is almost always a mistyped year.",
        "A year that puts the child outside your school's age range (2 to 25 "
        "unless your school has set its own) is refused, because it is almost "
        "always a mistyped year.",
    )],
    "student_number": [(
        "Optional unless your school has set a rule requiring one, in which "
        "case every row needs one and it must match the format.",
        "Optional unless the child's branch requires one, in which case every "
        "row needs one and it must match that branch's format. Where the "
        "branch issues numbers automatically, leave it blank and the next "
        "number is given.",
    )],
    "class": [(
        "A class this file fills past its capacity is imported and warned "
        "about.",
        "A class this file fills past its capacity is imported and warned "
        "about, unless your school never puts classes over capacity: then the "
        "rows past its last seat are refused.",
    )],
}


def _swap(apps, forward):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")
    template = ImportTemplate.objects.filter(code="students_v1").first()
    if template is None:
        return

    def swap(text, pairs):
        for old, new in pairs:
            a, b = (old, new) if forward else (new, old)
            text = text.replace(a, b)
        return text

    template.instructions = swap(template.instructions or "", INSTRUCTIONS)
    template.save(update_fields=["instructions"])
    for target_field, pairs in HELP.items():
        for column in ImportTemplateColumn.objects.filter(
            template=template, target_field=target_field,
        ):
            column.help_text = swap(column.help_text or "", pairs)
            column.save(update_fields=["help_text"])


def forwards(apps, schema_editor):
    _swap(apps, forward=True)


def backwards(apps, schema_editor):
    _swap(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0020_guardian_name_part_columns"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
