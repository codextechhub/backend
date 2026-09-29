"""Declare a school's own staff settings.

Eleven school-scoped definitions are created:

* ``staff.number.required``, ``.pattern``, ``.hint`` and ``.auto_issue``: the
  staff-number rule, which a branch may also hold as a whole (default: not
  required, any shape, nothing issued automatically);
* ``staff.starting_role``: the role key every new member of staff starts with
  (default ``teacher``);
* ``staff.documents.required``: the document types a school expects on every
  staff record, flagged and never enforced (default none);
* ``staff.self_editable_fields``: what a person may change about their own
  record (default middle name, date of birth, photo and phone);
* ``staff.hire.requires_approval``: whether a new hire waits for approval
  before being invited (default no);
* ``staff.leave.allowances``: days allowed per leave type per session (default
  no limit for any type);
* ``staff.leave.working_days``: the ISO weekdays leave is counted on (default
  Monday to Friday);
* ``staff.leave.exclude_closures``: whether a day the school is closed is left
  out of a leave request's count (default yes).

**Every default but two is the behaviour every school had before it could
choose.** The two that are not are the leave-counting rules: counting every
calendar day in a request's range, weekends and closures included, charged a
teacher for Saturdays, and no screen lets anybody correct the count. They are
applied when a request is filed or its dates are corrected; a count already
stored is never recomputed.

Declared by the staff module for the reason ``vs_students`` 0006 gives, and with
the same shape ``seed_config_catalogue`` gives them.

Reversible: the definitions and every school's and branch's values of them are
removed, and every school runs on the defaults again.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]
SCHOOL_AND_BRANCH = ["branch", "platform", "school"]

DEFINITIONS = [
    {
        "key": "staff.number.required",
        "label": "Staff Number Required",
        "description": (
            "Whether every new member of staff must be given a staff number, "
            "on the Add form, on the import and when an edit would blank one."
        ),
        "value_type": "BOOLEAN", "default_value": False,
        "allowed_scopes": SCHOOL_AND_BRANCH,
    },
    {
        "key": "staff.number.pattern",
        "label": "Staff Number Pattern",
        "description": (
            "A regular expression every new staff number must match. Anchored "
            "by the server, so it cannot match part of a longer number. Empty "
            "means any shape is accepted."
        ),
        "value_type": "STRING", "default_value": "",
        "allowed_scopes": SCHOOL_AND_BRANCH,
    },
    {
        "key": "staff.number.hint",
        "label": "Staff Number Hint",
        "description": (
            "The sentence shown under the staff number field, and quoted "
            "verbatim when a number is refused."
        ),
        "value_type": "STRING", "default_value": "",
        "allowed_scopes": SCHOOL_AND_BRANCH,
    },
    {
        "key": "staff.number.auto_issue",
        "label": "Issue Staff Numbers Automatically",
        "description": (
            "Whether a member of staff added with no staff number is given the "
            "next number in the school's series, or the branch's where the "
            "branch has its own rule. A number once held is never issued again."
        ),
        "value_type": "BOOLEAN", "default_value": False,
        "allowed_scopes": SCHOOL_AND_BRANCH,
    },
    {
        "key": "staff.starting_role",
        "label": "Starting Role For New Staff",
        "description": (
            "The key of the role every member of staff added at a live school "
            "starts with. It must be one of the school's active roles."
        ),
        "value_type": "STRING", "default_value": "teacher",
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.documents.required",
        "label": "Required Staff Documents",
        "description": (
            "The document types this school expects on every staff record. A "
            "record missing one is flagged; nothing is refused."
        ),
        "value_type": "JSON", "default_value": [],
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.self_editable_fields",
        "label": "Fields Staff May Edit Themselves",
        "description": (
            "The details a member of staff may change on their own record. The "
            "staff number, job title, employment type, hire and exit dates, "
            "email and posting are never among them."
        ),
        "value_type": "JSON",
        "default_value": ["middle_name", "date_of_birth", "photo", "phone"],
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.hire.requires_approval",
        "label": "Approve New Staff Before Inviting",
        "description": (
            "Whether a member of staff added at this school waits for the New "
            "staff approval ladder before their invitation is sent."
        ),
        "value_type": "BOOLEAN", "default_value": False,
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.leave.allowances",
        "label": "Leave Allowances",
        "description": (
            "Days of each leave type a member of staff may take in one academic "
            "session, keyed by leave type. A type left out has no limit. Leave "
            "past its allowance is still filed, marked for the approver."
        ),
        "value_type": "JSON", "default_value": {},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.leave.working_days",
        "label": "Working Days For Leave",
        "description": (
            "The weekdays a leave request counts, as ISO numbers (Monday is 1, "
            "Sunday is 7)."
        ),
        "value_type": "JSON", "default_value": [1, 2, 3, 4, 5],
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "staff.leave.exclude_closures",
        "label": "Leave Skips School Closures",
        "description": (
            "Whether a day the school calendar closes the school, at the "
            "person's branch or school-wide, is left out of a leave request's "
            "count."
        ),
        "value_type": "BOOLEAN", "default_value": True,
        "allowed_scopes": SCHOOL,
    },
]

KEYS = [row["key"] for row in DEFINITIONS]


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for row in DEFINITIONS:
        fields = {k: v for k, v in row.items() if k != "key"}
        definition_model.objects.get_or_create(
            key=row["key"],
            defaults={
                **fields, "validation_rules": {}, "sensitivity": "INTERNAL",
                "is_active": True,
            },
        )


def withdraw(apps, schema_editor):
    """Remove the definitions and every value of them.

    The values go first, for the reason ``vs_students`` 0006 gives: an orphan
    would resurrect a school's choice if the key were declared again.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(definition__key__in=KEYS).delete()
    definition_model.objects.filter(key__in=KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_staff", "0008_staff_number_unique_ignores_case"),
        ("vs_config", "0013_seed_display_timezone"),
    ]

    operations = [migrations.RunPython(declare, withdraw)]
