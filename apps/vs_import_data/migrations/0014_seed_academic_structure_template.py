"""The academic structure template.

Nine columns, and one row is one CLASS. A school holds a list of classes, not a
normalised hierarchy, so the programme and year group are named on each class
row and created the first time they appear. Asking for three files in
dependency order would be asking the school to normalise its own data before it
could hand it over, which is the work the import exists to remove.

There is no code column on any of the four things this creates. Codes are built
from the names by the same helper the screens use, because a school thinks in
names and a code column is a column of values somebody has to invent.

``Promotes To`` is here rather than left for later, and it is the column that
earns the file. ``Level.next_level`` null means two different things - pupils
leave here, or nobody wired this yet - and a structure built without it looks
complete right up to the end of the year, when promotion refuses to move
anybody. The validator warns on every level that says nothing.
"""
from django.db import migrations
from django.utils import timezone


TEMPLATE_DEFAULTS = {
    "name": "Academic Structure Import",
    "dataset_type": "academic_structure",
    "status": "active",
    "default_file_format": "xlsx",
    "description": (
        "Template for building a school's programmes, year groups and classes "
        "in one go, into the year it is running now."
    ),
    "instructions": (
        "ONE ROW PER CLASS. The programme and year group are named on each "
        "row, and each is created the first time it appears, so a school with "
        "sixty classes uploads sixty rows and gets its whole structure. "
        "\n\n"
        "WHAT IS REFUSED. A programme, year group and class name on every row. "
        "A year group placed under two different programmes, in this file or "
        "against what the school already runs, because a year group belongs to "
        "one programme and the usual cause is a mistyped cell. Two rows "
        "creating the same class in the same year group. Two year groups given "
        "the same position in one programme. A 'Promotes To' naming a year "
        "group that is not in this file. A year group that promotes into "
        "itself, or a chain that promotes in a circle - JSS1 to JSS2 and back "
        "to JSS1 - which would move pupils round for ever and which no screen "
        "can show you, because a screen sees one year group at a time. A "
        "branch this school does not have. A capacity that is not a number of "
        "seats, or is larger than 200. "
        "\n\n"
        "WHAT IS WARNED ABOUT, AND STILL IMPORTED. A year group that says "
        "nothing about where its pupils go next: end-of-year promotion will "
        "not move them until it does. A class already in the school, which is "
        "skipped rather than duplicated. A class name that does not look like "
        "it belongs to its year group, which usually means a row has slipped. "
        "\n\n"
        "PROMOTES TO takes the next year group's name exactly as it appears in "
        "the Level column, or the words 'Leaves school' for the last one. "
        "Level Order is the position of a year group inside its programme, 1 "
        "first; leave it blank and the order they appear in the file is used. "
        "Capacity and Branch are optional; leave Branch blank for a class the "
        "whole school shares. "
        "\n\n"
        "The whole file is imported together or not at all. Half a structure "
        "is worse than none, and re-uploading a corrected file builds onto "
        "what is there rather than creating a second copy."
    ),
    "allow_sample_row": True,
    "sample_row_data": {
        "Programme": "Junior Secondary",
        "Level": "JSS1",
        "Level Order": "1",
        "Promotes To": "JSS2",
        "Class": "JSS1 A",
        "Arm": "A",
        "Capacity": "30",
        "Branch": "",
        "Department": "",
    },
    "validation_rules": {
        "min_rows": 1,
        # A large school is around sixty classes. Five hundred is far past
        # anything real and still cheap to validate.
        "max_rows": 500,
        "allowed_file_formats": ["csv", "xlsx"],
    },
    "is_download_enabled": True,
}


COLUMNS = [
    {
        "column_name": "Programme", "target_field": "programme",
        "display_name": "Programme",
        "help_text": (
            "The stage this class belongs to: Nursery, Primary, Junior "
            "Secondary. Created the first time it appears."
        ),
        "data_type": "string", "is_required": True, "max_length": 100,
        "sample_value": "Junior Secondary", "column_order": 1,
    },
    {
        "column_name": "Level", "target_field": "level",
        "display_name": "Level",
        "help_text": (
            "The year group: JSS1, Primary 4. Created the first time it "
            "appears, under the programme on the same row."
        ),
        "data_type": "string", "is_required": True, "max_length": 60,
        "sample_value": "JSS1", "column_order": 2,
    },
    {
        "column_name": "Level Order", "target_field": "level_order",
        "display_name": "Level Order",
        "help_text": (
            "Where this year group sits inside its programme, 1 for the first. "
            "Leave blank to use the order the rows appear in."
        ),
        "data_type": "integer", "is_required": False,
        "sample_value": "1", "column_order": 3,
    },
    {
        "column_name": "Promotes To", "target_field": "promotes_to",
        "display_name": "Promotes To",
        "help_text": (
            "The year group pupils move into at the end of the year, written "
            "exactly as it appears in the Level column. Write 'Leaves school' "
            "for the last one. Left blank, promotion will not move this year "
            "group at all."
        ),
        "data_type": "string", "is_required": False, "max_length": 60,
        "sample_value": "JSS2", "column_order": 4,
    },
    {
        "column_name": "Class", "target_field": "class_name",
        "display_name": "Class",
        "help_text": "The class pupils sit in: JSS1 A.",
        "data_type": "string", "is_required": True, "max_length": 60,
        "sample_value": "JSS1 A", "column_order": 5,
    },
    {
        "column_name": "Arm", "target_field": "arm",
        "display_name": "Arm",
        "help_text": (
            "A, B, Gold. Taken from the end of the class name if left blank."
        ),
        "data_type": "string", "is_required": False, "max_length": 30,
        "sample_value": "A", "column_order": 6,
    },
    {
        "column_name": "Capacity", "target_field": "capacity",
        "display_name": "Capacity",
        "help_text": (
            "How many pupils the class holds. Leave blank for no limit. This "
            "is what refuses an over-full class when children are placed."
        ),
        "data_type": "integer", "is_required": False,
        "sample_value": "30", "column_order": 7,
    },
    {
        "column_name": "Branch", "target_field": "branch",
        "display_name": "Branch",
        "help_text": (
            "The branch that runs this class, written exactly as it appears in "
            "Branches. Leave blank for a class the whole school shares, which "
            "is the usual case."
        ),
        "data_type": "string", "is_required": False, "max_length": 120,
        "sample_value": "", "column_order": 8,
    },
    {
        "column_name": "Department", "target_field": "department",
        "display_name": "Department",
        "help_text": (
            "Optional faculty grouping for the programme: Sciences, Arts. "
            "Created the first time it appears."
        ),
        "data_type": "string", "is_required": False, "max_length": 100,
        "sample_value": "", "column_order": 9,
    },
]


def seed(apps, schema_editor):
    ImportTemplate = apps.get_model("vs_import_data", "ImportTemplate")
    ImportTemplateColumn = apps.get_model("vs_import_data", "ImportTemplateColumn")

    template, _ = ImportTemplate.objects.update_or_create(
        code="academic_structure_v1",
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
        ("vs_import_data", "0013_alter_importbatch_dataset_type_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
