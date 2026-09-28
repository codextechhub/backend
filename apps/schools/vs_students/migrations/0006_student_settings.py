"""Declare a school's student settings, and let a branch hold its own admission rule.

Seven definitions are created and three widened:

* ``students.age.min_years`` and ``students.age.max_years``: the age range a
  birth date is checked against (default 2 to 25);
* ``students.documents.required``: the documents a checklist prompts for
  (default the birth certificate);
* ``students.enrolment.required_fields``: optional enrolment fields the school
  makes required (default none);
* ``students.capacity.mode``: what a full class does, WARN, HARD or OFF
  (default WARN);
* ``students.capacity.default``: the size a new class is given (default none,
  meaning no limit);
* ``students.admission_number.auto_issue``: whether a blank admission number is
  filled with the next in the series (default off);
* the three existing ``students.admission_number.*`` definitions gain branch
  scope, so a branch can hold a rule of its own.

**Every default is the behaviour every school had before it could choose**, so
this migration changes nothing for anybody until a school saves a setting.

**Declared here rather than in vs_config.** vs_config usually declares its own
catalogue rows, because the app that consumes a setting does not own the row
that declares it. These are declared by the students module because they are
seven rows of one screen that only this module reads, and because keeping them
out of vs_config's migration chain leaves that chain to the settings that are
genuinely the platform's. ``seed_config_catalogue`` declares the same rows with
the same shape, so a database built either way ends up alike.

**The admission definitions are created here if missing.** They were declared
only by ``seed_config_catalogue``, so a database that has never been seeded has
no rule to widen; creating them with the seeder's shape is what lets a branch
rule be written there at all.
"""

from django.db import migrations


SCHOOL = ["platform", "school"]
SCHOOL_AND_BRANCH = ["branch", "platform", "school"]

DEFINITIONS = [
    {
        "key": "students.age.min_years",
        "label": "Youngest Enrolment Age",
        "description": (
            "The youngest a student at this school can be, in whole years. A "
            "birth date that makes a child younger is refused as a mistyped "
            "year, on the enrolment form, the edit form and the import."
        ),
        "value_type": "INTEGER", "default_value": 2,
        "validation_rules": {"min": 0, "max": 99}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.age.max_years",
        "label": "Oldest Enrolment Age",
        "description": (
            "The oldest a student at this school can be, in whole years. A "
            "birth date that makes a child older is refused as a mistyped year."
        ),
        "value_type": "INTEGER", "default_value": 25,
        "validation_rules": {"min": 0, "max": 99}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.documents.required",
        "label": "Required Student Documents",
        "description": (
            "The documents a student's checklist marks as required. A prompt, "
            "never a gate: a child is enrolled whether or not they are attached."
        ),
        "value_type": "JSON", "default_value": ["BIRTH_CERTIFICATE"],
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.enrolment.required_fields",
        "label": "Required Enrolment Fields",
        "description": (
            "Optional enrolment fields this school requires, such as nationality "
            "or home address. Enforced when a student is enrolled or imported, "
            "and when an edit would blank one; an older record missing one is "
            "never blocked."
        ),
        "value_type": "JSON", "default_value": [],
        "validation_rules": {}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.capacity.mode",
        "label": "Class Capacity Rule",
        "description": (
            "What a full class does. WARN refuses until staff choose to go "
            "ahead; HARD refuses with no override; OFF does not check."
        ),
        "value_type": "CHOICE", "default_value": "WARN",
        "validation_rules": {"choices": ["WARN", "HARD", "OFF"]},
        "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.capacity.default",
        "label": "Default Class Size",
        "description": (
            "The capacity a new class is given when it is created without one. "
            "Empty means no limit."
        ),
        "value_type": "INTEGER", "default_value": None,
        "validation_rules": {"min": 1, "max": 500}, "allowed_scopes": SCHOOL,
    },
    {
        "key": "students.admission_number.auto_issue",
        "label": "Issue Admission Numbers Automatically",
        "description": (
            "Whether a student enrolled or confirmed with no admission number "
            "is given the next number in the school's series, or the branch's "
            "where the branch has its own rule."
        ),
        "value_type": "BOOLEAN", "default_value": False,
        "validation_rules": {}, "allowed_scopes": SCHOOL_AND_BRANCH,
    },
]

#: The seeder's shape for the three admission definitions, used only where one
#: is missing.
ADMISSION = [
    {
        "key": "students.admission_number.required",
        "label": "Admission Number Required",
        "description": (
            "Whether every student at this school must be given an admission "
            "number when they are enrolled."
        ),
        "value_type": "BOOLEAN", "default_value": False,
    },
    {
        "key": "students.admission_number.pattern",
        "label": "Admission Number Pattern",
        "description": (
            "A regular expression every admission number at this school must "
            "match. Anchored by the server, so it cannot match part of a longer "
            "number. Empty means any shape is accepted."
        ),
        "value_type": "STRING", "default_value": "",
    },
    {
        "key": "students.admission_number.hint",
        "label": "Admission Number Hint",
        "description": (
            "The sentence shown under the admission number field, and quoted "
            "verbatim when a number is refused. This is the only one of the "
            "three a person reads, which is why a refusal never quotes the "
            "pattern."
        ),
        "value_type": "STRING", "default_value": "",
    },
]

NEW_KEYS = [row["key"] for row in DEFINITIONS]
ADMISSION_KEYS = [row["key"] for row in ADMISSION]


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for row in DEFINITIONS:
        fields = {k: v for k, v in row.items() if k != "key"}
        definition_model.objects.get_or_create(
            key=row["key"],
            defaults={**fields, "sensitivity": "INTERNAL", "is_active": True},
        )
    for row in ADMISSION:
        fields = {k: v for k, v in row.items() if k != "key"}
        definition, created = definition_model.objects.get_or_create(
            key=row["key"],
            defaults={
                **fields, "validation_rules": {},
                "allowed_scopes": SCHOOL_AND_BRANCH,
                "sensitivity": "INTERNAL", "is_active": True,
            },
        )
        if not created and "branch" not in (definition.allowed_scopes or []):
            definition.allowed_scopes = sorted(
                {*(definition.allowed_scopes or []), "branch"},
            )
            definition.save(update_fields=["allowed_scopes"])


def withdraw(apps, schema_editor):
    """Remove the new definitions and every branch's own admission rule.

    Branch values go as well as the scope, because resolution does not consult
    ``allowed_scopes``: a branch row left behind would keep overriding the
    school's rule with nothing able to write or remove it. The new definitions'
    values go with them for the reason vs_config's own seed migrations give:
    an orphan would resurrect a school's choice if the key were re-declared.
    """
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    value_model.objects.filter(
        definition__key__in=ADMISSION_KEYS, scope_key__startswith="branch:",
    ).delete()
    for definition in definition_model.objects.filter(key__in=ADMISSION_KEYS):
        if "branch" in (definition.allowed_scopes or []):
            definition.allowed_scopes = [
                s for s in definition.allowed_scopes if s != "branch"
            ]
            definition.save(update_fields=["allowed_scopes"])
    value_model.objects.filter(definition__key__in=NEW_KEYS).delete()
    definition_model.objects.filter(key__in=NEW_KEYS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("vs_students", "0005_guardian_name_parts"),
        ("vs_config", "0011_seed_workflow_notifications"),
    ]

    operations = [
        migrations.RunPython(declare, withdraw),
    ]
