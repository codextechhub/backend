"""A vendor assessment's date has no default.

The default was ``datetime.date.today``, the server's UTC day: the day before
for the first hour after midnight in Lagos. Every write names the day (the
tenant's, through ``vs_config.clock.tenant_today``, unless the assessor chose
another). The default lived only in Python, so this changes no column and no
row, and it reverses to the old default.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_procurement", "0041_opening_vendor_bills"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendorassessment",
            name="assessment_date",
            field=models.DateField(),
        ),
    ]
