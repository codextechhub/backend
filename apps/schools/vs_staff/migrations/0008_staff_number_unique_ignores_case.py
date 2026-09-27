"""A staff ID is unique at its school whatever its case.

"BS/stf/0001" and "BS/STF/0001" were two different IDs to the old constraint,
so the same number typed twice in different case gave two people one ID.
Admission numbers have always compared without case; staff IDs now do too.

Before the new constraint goes on, any school already holding two IDs that
differ only in case is named, so the migration stops with a sentence rather
than an IntegrityError. Reversible: the old, case-sensitive constraint returns.
"""
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models
from django.db.models.functions import Lower


def refuse_case_duplicates(apps, schema_editor):
    StaffProfile = apps.get_model("vs_staff", "StaffProfile")
    clashes = (
        StaffProfile.objects.exclude(staff_number="")
        .annotate(n=Lower("staff_number"))
        .values("tenant_id", "n")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
    )
    found = [f"tenant {c['tenant_id']}: {c['n']}" for c in clashes]
    if found:
        raise RuntimeError(
            "These staff IDs differ only in case at the same school; give each "
            "person their own before migrating: " + "; ".join(found),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("vs_staff", "0007_school_organogram"),
        ("vs_tenants", "0010_remove_branch__type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(refuse_case_duplicates, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="staffprofile",
            name="uq_staff_number_per_tenant",
        ),
        migrations.AddConstraint(
            model_name="staffprofile",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("staff_number"),
                models.F("tenant"),
                condition=models.Q(("staff_number", ""), _negated=True),
                name="uq_staff_number_per_tenant_ci",
            ),
        ),
    ]
