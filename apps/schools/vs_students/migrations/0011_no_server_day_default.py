"""A student's enrolment date and a placement's or status change's effective date
have no default.

The default was the server's UTC day, the day before for the first hour after
midnight in Lagos and wrong for a branch keeping its own zone. Every write now
names the day (its branch's, through ``vs_config.clock.branch_today``). The
default lived only in Python, so this changes no column and no row, and it
reverses to the old default.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0010_promotion_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="classenrolment",
            name="effective_date",
            field=models.DateField(),
        ),
        migrations.AlterField(
            model_name="student",
            name="enrolment_date",
            field=models.DateField(),
        ),
        migrations.AlterField(
            model_name="studentstatuslog",
            name="effective_date",
            field=models.DateField(),
        ),
    ]
