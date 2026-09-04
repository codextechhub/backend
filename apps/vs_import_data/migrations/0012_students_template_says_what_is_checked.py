"""Tell a school what the students import actually refuses, before it uploads.

The template's guidance described the shape of the file. It did not describe
the checks, so the only way to learn that a mistyped birth year is refused, or
that two rows sharing a guardian email become one household, was to upload and
read the report. That is the wrong order: the school builds the spreadsheet
first, and every rule it does not know about is a round trip.

The rules themselves live in ``schools.vs_students.imports``, where both the
validation pass and the write pass read them. This migration only writes down
what they are. A rule changed there and not repeated here leaves the guidance
stale rather than the import wrong, which is the right way round.

Columns are keyed on ``target_field``, exactly as ``0011`` keys them, so this
also repairs a column whose heading has drifted.
"""
from django.db import migrations


INSTRUCTIONS = (
    "One row per child. Every student is created on the roll, and placed in "
    "the class named on their row. A row with no class still imports and the "
    "student waits under Classes and transfers. "
    "\n\n"
    "WHAT IS REFUSED. A name, date of birth, gender, guardian name and "
    "guardian phone on every row. A date of birth that is in the future, or "
    "whose year would make the child under 2 or over 25, because that is "
    "almost always a mistyped year. An admission date in the future, or one "
    "before the child was born. A class this school does not run in the year "
    "it is running now, or one that belongs to a different branch from the "
    "student. An admission number that breaks your school's format, that "
    "another student already holds, or that appears twice in this file. A "
    "guardian email that is not an address, or a phone with too few digits to "
    "ring. Any value too long for its column, which is usually two columns "
    "that have shifted. "
    "\n\n"
    "WHAT IS WARNED ABOUT, AND STILL IMPORTED. A child already on the roll "
    "with the same name and date of birth. A class that this file fills past "
    "its capacity, naming the class and the count. A guardian contact that "
    "another row, or another guardian already at this school, uses under a "
    "different name: those children join that one household and the name in "
    "your row is not recorded. A relationship this school does not hold, "
    "which is imported as Other. A digit inside a name. "
    "\n\n"
    "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND. Give siblings the "
    "same contact and they become one household with one guardian; give "
    "unrelated children different contacts, or they are merged into one. "
    "\n\n"
    "Dates are YYYY-MM-DD. A date written 12/03/2014 is read as 12 March. "
    "Branch is required on every row only if your school has more than one. "
    "Nothing is written until validation passes and you confirm."
)

#: target_field -> the help this column now carries.
HELP = {
    "date_of_birth": (
        "YYYY-MM-DD. A day-first date such as 12/03/2014 is read as 12 March. "
        "A year that would make the child under 2 or over 25 is refused, "
        "because it is almost always a mistyped year."
    ),
    "admission_date": (
        "YYYY-MM-DD, the day the child joined the school. Today if left blank. "
        "Cannot be in the future or before the date of birth."
    ),
    "student_number": (
        "Your school's own number. Optional unless your school has set a rule "
        "requiring one, in which case every row needs one and it must match "
        "the format. Must not already belong to another student, or appear "
        "twice in this file."
    ),
    "class": (
        "A class your school already runs in the year it is running now, "
        "written exactly as it appears in Academic Structure, and at the same "
        "branch as the child. Blank leaves the student unplaced. A class this "
        "file fills past its capacity is imported and warned about."
    ),
    "guardian_email": (
        "How siblings are recognised, and the address any parent account is "
        "issued to. Two rows carrying the same address become one household. "
        "Give unrelated children different addresses."
    ),
    "guardian_phone": (
        "A number the school can reach. Guardians are matched on it after "
        "email, so two rows sharing a number also become one household."
    ),
}


def describe(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template = ImportTemplate.objects.filter(code="students_v1").first()
    if template is None:
        return
    template.instructions = INSTRUCTIONS
    template.save(update_fields=["instructions"])

    for target_field, help_text in HELP.items():
        ImportTemplateColumn.objects.filter(
            template=template, target_field=target_field,
        ).update(help_text=help_text)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0011_seed_students_template"),
    ]

    operations = [
        migrations.RunPython(describe, migrations.RunPython.noop),
    ]
