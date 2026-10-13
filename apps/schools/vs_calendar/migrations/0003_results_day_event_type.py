"""Add the Results day event type.

The event type is a stored choice, so the schema change only widens the
allowed values. The data step brings the declared default of
``calendar.closes_school_by_type`` up to the seven event types: a results day
does not close the school. A school's own saved answer is never touched, and a
saved answer that predates the new type reads the default for it.

Reversible: the default returns to the six original types.
"""

from django.db import migrations, models

KEY = "calendar.closes_school_by_type"
SIX = {
    "HOLIDAY": True, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
    "SCHOOL_EVENT": False, "PTA": False, "SPORTS": False,
}
SEVEN = {**SIX, "RESULTS_DAY": False}


def widen_default(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.filter(key=KEY, default_value=SIX).update(
        default_value=SEVEN,
    )


def narrow_default(apps, schema_editor):
    definition_model = apps.get_model("vs_config", "ConfigurationDefinition")
    definition_model.objects.filter(key=KEY, default_value=SEVEN).update(
        default_value=SIX,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_calendar", "0002_calendar_and_timetable_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="calendarevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("HOLIDAY", "Public holiday"),
                    ("MIDTERM_BREAK", "Mid-term break"),
                    ("EXAM_PERIOD", "Exam period"),
                    ("SCHOOL_EVENT", "School event"),
                    ("PTA", "PTA"),
                    ("SPORTS", "Sports day"),
                    ("RESULTS_DAY", "Results day"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.RunPython(widen_default, narrow_default),
    ]
