"""A school's date format and clock, and a branch's own time zone.

Two school-scoped definitions are declared:

* ``display.date_format``: how a date is written for the school,
  ``D_MMM_YYYY`` ("29 Sep 2026", the default), ``DD_MM_YYYY`` ("29/09/2026")
  or ``YYYY_MM_DD`` ("2026-09-29"). There is no month-first format: a date
  like 03/04/2026 would mean two different days to two readers.
* ``display.clock``: ``H12`` ("8:00 am", the default) or ``H24`` ("08:00").

Both are one choice for the whole school, so they allow ``platform`` and
``school`` scope only.

``display.timezone`` gains ``branch`` scope. The school's zone stays the
default, and a branch that sits in another zone (a Nairobi branch of a Lagos
school) keeps its own, so the day its students, staff and invoices are judged
on is the branch's. Its description loses the promise that documents print
local time, which they do not yet.

Every default is what a school already had, so nothing changes for anybody
until a school or branch saves a value.

Reversible: the two definitions and every value of them are removed; every
branch value of the time zone is removed and the definition goes back to
platform and school scope, so each branch follows its school again.
"""

from django.db import migrations


TIME_ZONE = "display.timezone"

TIME_ZONE_DESCRIPTION = (
    "The IANA time zone this school keeps its calendar in, such as "
    "Africa/Lagos. It decides which day \"today\" is for due dates, overdue "
    "checks, attendance and the calendar. A branch in another zone may keep "
    "its own, and anything belonging to that branch then follows the "
    "branch's day. The platform value is the default for every school."
)

PREVIOUS_TIME_ZONE_DESCRIPTION = (
    "The IANA time zone this school keeps its calendar in, such as "
    "Africa/Lagos. It decides which day \"today\" is for due dates, "
    "overdue checks, attendance and the calendar, and the local time "
    "shown on documents. The platform value is the default for every "
    "school."
)

DEFINITIONS = [
    {
        "key": "display.date_format",
        "label": "Date Format",
        "description": (
            "How the school's screens write a date: D_MMM_YYYY (29 Sep 2026), "
            "DD_MM_YYYY (29/09/2026) or YYYY_MM_DD (2026-09-29)."
        ),
        "value_type": "CHOICE", "default_value": "D_MMM_YYYY",
        "validation_rules": {"choices": ["D_MMM_YYYY", "DD_MM_YYYY", "YYYY_MM_DD"]},
        "allowed_scopes": ["platform", "school"],
    },
    {
        "key": "display.clock",
        "label": "Clock",
        "description": (
            "How the school's screens write a time of day: H12 (8:00 am) or "
            "H24 (08:00)."
        ),
        "value_type": "CHOICE", "default_value": "H12",
        "validation_rules": {"choices": ["H12", "H24"]},
        "allowed_scopes": ["platform", "school"],
    },
]


def declare(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    for spec in DEFINITIONS:
        definition_model.objects.get_or_create(
            key=spec["key"],
            defaults={
                **{name: value for name, value in spec.items() if name != "key"},
                "sensitivity": "INTERNAL",
                "is_active": True,
            },
        )
    zone = definition_model.objects.filter(key=TIME_ZONE).first()
    if zone is not None:
        zone.allowed_scopes = sorted({*(zone.allowed_scopes or []), "branch"})
        zone.description = TIME_ZONE_DESCRIPTION
        zone.save(update_fields=["allowed_scopes", "description", "updated_at"])


def withdraw(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    value_model = apps.get_model("vs_config", "ConfigurationValue")
    keys = [spec["key"] for spec in DEFINITIONS]
    value_model.objects.filter(definition__key__in=keys).delete()
    definition_model.objects.filter(key__in=keys).delete()
    value_model.objects.filter(
        definition__key=TIME_ZONE, branch__isnull=False,
    ).delete()
    zone = definition_model.objects.filter(key=TIME_ZONE).first()
    if zone is not None:
        zone.allowed_scopes = [
            scope for scope in (zone.allowed_scopes or []) if scope != "branch"
        ]
        zone.description = PREVIOUS_TIME_ZONE_DESCRIPTION
        zone.save(update_fields=["allowed_scopes", "description", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("vs_config", "0013_seed_display_timezone"),
    ]

    operations = [
        migrations.RunPython(declare, withdraw),
    ]
