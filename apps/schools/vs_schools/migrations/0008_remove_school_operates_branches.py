"""Drop ``School.operates_branches``: the branches themselves are the answer.

The flag was a stored proxy for "does this school run more than one site",
written at creation and correctable afterwards. Every school is now created
with at least one branch, so ``tenant.branches`` can be counted directly, and a
stored count that nothing keeps in step with the rows it describes is a second
source of truth waiting to disagree with the first.

Reversing this migration puts the column back with its original default of
False. It does not put the values back: the truthful reconstruction is the
branch count, and 0006 already showed how to derive it, so a school that needs
the flag again should be backfilled from its rows rather than from a guess this
migration would have had to store somewhere.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("vs_schools", "0007_alter_school_status"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="school",
            name="operates_branches",
        ),
    ]
