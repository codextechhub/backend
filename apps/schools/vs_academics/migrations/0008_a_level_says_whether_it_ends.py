"""A level says whether it ends, instead of a null saying two things.

Deliberately NOT backfilled. Every existing level gets False, meaning "nobody
has said" - which is the truth. Marking each programme's last level terminal
would be a guess, and guessing is the exact failure the field exists to stop:
the wrong guess graduates a year group and says nothing.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_academics", "0007_subject_is_catalogue"),
        ("vs_tenants", "0008_alter_branch_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="level",
            name="is_terminal",
            field=models.BooleanField(
                default=False, help_text="Pupils leave the school after this level."
            ),
        ),
        migrations.AddConstraint(
            model_name="level",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("is_terminal", False),
                    ("next_level__isnull", True),
                    _connector="OR",
                ),
                name="ck_academic_level_terminal_has_no_next",
            ),
        ),
    ]
