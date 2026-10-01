"""The day a suspended pupil is expected back.

Nullable on every status log row, and meaningful only on a suspension, where a
null is a suspension that stands until a person lifts it rather than an unknown
date. Every row that exists before this migration keeps a null, which is
exactly what those suspensions were: no end date was ever collected for them,
so none of them is swept by the automatic return.

The check constraint holds the one invariant a date here has: a suspension ends
after it starts, so the shortest one begins on Monday and ends on Tuesday. It
passes on a null. The partial index carries the automatic return's sweep, which
reads the whole platform and must not read a decade of every school's status
changes to find the few suspensions due today.

Reversible: the column, its index and its constraint are dropped, and a
suspension stands until somebody lifts it again.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vs_students", "0012_suspension_notice_setting"),
        ("vs_tenants", "0011_platform_tenant_lagos_branch"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="studentstatuslog",
            name="return_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="studentstatuslog",
            index=models.Index(
                condition=models.Q(("return_date__isnull", False)),
                fields=["to_status", "return_date"],
                name="idx_statuslog_due_return",
            ),
        ),
        migrations.AddConstraint(
            model_name="studentstatuslog",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("return_date__isnull", True),
                    ("return_date__gt", models.F("effective_date")),
                    _connector="OR",
                ),
                name="ck_statuslog_return_after_effective",
            ),
        ),
    ]
