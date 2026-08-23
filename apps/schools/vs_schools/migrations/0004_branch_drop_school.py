"""Take the school off Branch, and re-key its indexes on the tenant.

Phase D of docs/architecture/school-decoupling-scope.md, first half. This is
the only migration in the phase that touches the database at all; everything
after it is Django model state.

Two things happen here, and both must happen while ``Branch`` still lives in
``vs_schools``, because that is the app whose migration state owns the model
until the state-only move lands:

1. The three composite indexes are re-keyed from ``school`` to ``tenant``.
   They are given the exact names the moved model will declare, so the state
   move that follows is a no-op against the database rather than a rebuild.
2. The ``school_id`` column and its foreign key are dropped.

The drop is written as three operations rather than one so that it reverses.
``RemoveField`` alone reverses into an ``AddField`` of a non-nullable foreign
key, which no populated table will accept; nullable-add, backfill, then set
NOT NULL is the same three-step shape the phase B tenant column used, run in
the other direction. The reverse backfill reads ``tenant.school_profile``,
which is exactly where the value came from: ``School.tenant`` is a
non-nullable OneToOneField, so a school tenant has exactly one school and the
round trip is lossless.

A tenant that is not a school (the codex PLATFORM tenant, or a future
non-school product) has no ``school_profile``. It also cannot own a branch
today, and if it ever did, reversing this migration could not invent a school
for it - the reverse raises rather than writing a wrong row.
"""
from django.db import migrations, models
import django.db.models.deletion


def restore_branch_school(apps, schema_editor):
    """Reverse only: put each branch back under its tenant's school."""
    Branch = apps.get_model("vs_schools", "Branch")
    School = apps.get_model("vs_schools", "School")

    schools = dict(School.objects.values_list("tenant_id", "pk"))
    orphans = []
    for branch_id, tenant_id in Branch.objects.values_list("pk", "tenant_id"):
        school_id = schools.get(tenant_id)
        if school_id is None:
            orphans.append(branch_id)
            continue
        Branch.objects.filter(pk=branch_id).update(school_id=school_id)

    if orphans:
        raise RuntimeError(
            "Cannot restore Branch.school: branches "
            f"{sorted(orphans)} belong to tenants with no school profile."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("vs_schools", "0003_branch_tenant"),
    ]

    operations = [
        # --- re-key the indexes onto the tenant -------------------------------
        # The new names are the ones Django derives for vs_tenants.Branch. They
        # are identical because the index name is a hash of the table and the
        # columns, and neither changes.
        migrations.RemoveIndex(
            model_name="branch",
            name="vs_schools__school__38f3c1_idx",
        ),
        migrations.RemoveIndex(
            model_name="branch",
            name="vs_schools__school__e52510_idx",
        ),
        migrations.RemoveIndex(
            model_name="branch",
            name="vs_schools__school__b13fda_idx",
        ),
        migrations.AddIndex(
            model_name="branch",
            index=models.Index(
                fields=["tenant", "is_main"], name="vs_schools__tenant__6bef02_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="branch",
            index=models.Index(
                fields=["tenant", "status"], name="vs_schools__tenant__b47bb3_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="branch",
            index=models.Index(
                fields=["tenant", "code"], name="vs_schools__tenant__457ea7_idx"
            ),
        ),
        # --- drop the school link ---------------------------------------------
        migrations.AlterField(
            model_name="branch",
            name="school",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="branches",
                to="vs_schools.school",
            ),
        ),
        migrations.RunPython(
            migrations.RunPython.noop,
            restore_branch_school,
        ),
        migrations.RemoveField(
            model_name="branch",
            name="school",
        ),
    ]
