"""The students and guardians templates' guidance follows each school's guardian rules.

The guidance was written when every school matched guardians the same way,
asked for no guardian email, needed one guardian per child and recorded the
same eight relationships. A school now sets each of those in Settings,
Guardians, and a template is one row shared by every school, so it can no
longer state one school's rules:

* guardians are matched on email then phone, or on email alone;
* a guardian email may be required, which the import refuses a row without;
* a school may ask for more than one guardian per child, which the students
  import warns about on every row, because it carries one;
* a school's own relationships are recognised as well as the fixed eight.

Each sentence is swapped only where it still reads as it was written, so a
template whose guidance has been edited since is left alone. Reversible: the
sentences are swapped back.
"""
from django.db import migrations

#: template code -> [(as written, as it now reads)] for its instructions.
INSTRUCTIONS = {
    "students_v1": [
        (
            "A name, date of birth, gender, guardian name and guardian phone "
            "on every row.",
            "A name, date of birth, gender, guardian name and guardian phone "
            "on every row, and a guardian email too where your school requires "
            "one (Settings, Guardians).",
        ),
        (
            "A relationship this school does not hold, which is imported as "
            "Other.",
            "A relationship that is neither one of the fixed eight nor one "
            "your school has added in Settings, Guardians, which is imported "
            "as Other. Every row, where your school asks for more than one "
            "guardian per child, because this file carries one and the others "
            "are added afterwards.",
        ),
        (
            "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND.",
            "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND, or on "
            "email alone where your school has chosen that in Settings, "
            "Guardians.",
        ),
    ],
    "guardians_v1": [
        (
            "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND.",
            "GUARDIANS ARE MATCHED ON EMAIL FIRST AND PHONE SECOND, or on "
            "email alone where your school has chosen that in Settings, "
            "Guardians.",
        ),
        (
            "WHAT IS REFUSED. A guardian name and a phone number on every row.",
            "WHAT IS REFUSED. A guardian name and a phone number on every row, "
            "and an email for a guardian new to the school where your school "
            "requires one.",
        ),
        (
            "A relationship this school does not record, which is imported as "
            "Other.",
            "A relationship that is neither one of the fixed eight nor one "
            "your school has added in Settings, Guardians, which is imported "
            "as Other.",
        ),
    ],
}

_FIXED_RELATIONSHIPS = (
    "Uncle, Aunt, Grandparent, Legal guardian, Sibling or Other. Anything else "
    "is imported as Other."
)
_RELATIONSHIPS_NOW = (
    "Uncle, Aunt, Grandparent, Legal guardian, Sibling or Other, or a "
    "relationship your school has added in Settings, Guardians. Anything else "
    "is imported as Other."
)

#: template code -> target_field -> [(as written, as it now reads)] for column help.
HELP = {
    "students_v1": {
        "guardian_email": [(
            "Give unrelated children different addresses.",
            "Give unrelated children different addresses. Required on every "
            "row where your school requires a guardian email.",
        )],
        "guardian_phone": [(
            "so two rows sharing a number also become one household.",
            "so two rows sharing a number also become one household, unless "
            "your school matches guardians on email alone.",
        )],
        "guardian_relationship": [(_FIXED_RELATIONSHIPS, _RELATIONSHIPS_NOW)],
    },
    "guardians_v1": {
        "guardian_email": [(
            "and the address any parent account is issued to.",
            "and the address any parent account is issued to. Required for a "
            "guardian new to the school where your school requires a guardian "
            "email.",
        )],
        "guardian_phone": [(
            "so two rows sharing a number are treated as one person.",
            "so two rows sharing a number are treated as one person, unless "
            "your school matches guardians on email alone.",
        )],
        "relationship": [(_FIXED_RELATIONSHIPS, _RELATIONSHIPS_NOW)],
    },
}


def _swap(apps, forward):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    def swap(text, pairs):
        for old, new in pairs:
            # Some sentences grow by a clause, so the old one is inside the new.
            if forward and new in text:
                continue
            a, b = (old, new) if forward else (new, old)
            text = text.replace(a, b)
        return text

    for code, pairs in INSTRUCTIONS.items():
        template = ImportTemplate.objects.filter(code=code).first()
        if template is None:
            continue
        template.instructions = swap(template.instructions or "", pairs)
        template.save(update_fields=["instructions"])
        for target_field, help_pairs in HELP[code].items():
            for column in ImportTemplateColumn.objects.filter(
                template=template, target_field=target_field,
            ):
                column.help_text = swap(column.help_text or "", help_pairs)
                column.save(update_fields=["help_text"])


def forwards(apps, schema_editor):
    _swap(apps, forward=True)


def backwards(apps, schema_editor):
    _swap(apps, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("vs_import_data", "0021_students_template_follows_school_rules"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
