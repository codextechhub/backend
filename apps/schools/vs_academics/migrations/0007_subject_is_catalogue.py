"""Subject stops belonging to a year; where it is TAUGHT still does.

The reasoning is in 0006, which merged each school's per-year copies into one
row first. This is the shape change that merge made possible.
"""
from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):

    dependencies = [
        ("vs_academics", "0006_merge_the_per_year_subjects"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="subject", name="uq_academic_subject_name",
        ),
        migrations.RemoveConstraint(
            model_name="subject", name="uq_academic_subject_code",
        ),
        migrations.RemoveField(model_name="subject", name="session"),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(
                Lower("name"), "tenant", name="uq_academic_subject_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(
                Lower("code"), "tenant", name="uq_academic_subject_code",
            ),
        ),
    ]
