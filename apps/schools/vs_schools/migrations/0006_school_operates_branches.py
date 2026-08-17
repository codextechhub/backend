"""Record whether a school runs more than one site.

The field defaults to False because branch-optional schools are real and a
school that never intends a second site must not be handed a branch-setup step
it can never finish. That default would be wrong for schools that already have
branches, though, so those are backfilled to True in the same migration: the
evidence is already in the database, and leaving it uncollected would mean every
existing multi-site school looked single-site to onboarding.

Reversing the migration drops the column, so the backfill needs no inverse.
"""
from django.db import migrations, models


def backfill_operates_branches(apps, schema_editor):
    School = apps.get_model("vs_schools", "School")
    Branch = apps.get_model("vs_tenants", "Branch")

    tenant_ids = set(Branch.objects.values_list("tenant_id", flat=True))
    if not tenant_ids:
        return
    School.objects.filter(tenant_id__in=tenant_ids).update(operates_branches=True)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tenants", "0004_move_branch_from_vs_schools"),
        ("vs_schools", "0005_move_branch_to_vs_tenants"),
    ]

    operations = [
        migrations.AddField(
            model_name="school",
            name="operates_branches",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Whether this school runs more than one site. Onboarding "
                    "reads this to decide whether branch setup is a step the "
                    "school must complete."
                ),
            ),
        ),
        migrations.RunPython(
            backfill_operates_branches,
            migrations.RunPython.noop,
        ),
    ]
