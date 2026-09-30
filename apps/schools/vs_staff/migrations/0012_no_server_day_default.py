"""An appointment's start date has no default.

The default was the server's UTC day; every write now names the day at the
person's posting (``vs_config.clock.branch_today``). The default lived only in
Python, so this changes no column and no row, and it reverses to the old
default.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0011_invited_at_go_live"),
    ]

    operations = [
        migrations.AlterField(
            model_name="staffpositionassignment",
            name="start_date",
            field=models.DateField(),
        ),
    ]
