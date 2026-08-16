"""Give Branch its own tenant, and the uniqueness it always claimed to have.

Phase B of docs/architecture/school-decoupling-scope.md. The tenant column is
added in the three separate operations a populated table requires: nullable
field, backfill, then NOT NULL. A PROTECT foreign key with no default cannot be
added NOT NULL in one step.

The backfill is an ORM ``RunPython`` over the historical model, not raw SQL, so
it needs no vendor branch. ``School.tenant`` is a non-nullable OneToOneField and
``Branch.school`` is non-nullable, so every branch resolves to exactly one
tenant and the backfill cannot leave a null behind.

The de-duplication step before the constraints is deliberate. This table has
never had any uniqueness, so no database can be assumed clean, and an
``AddConstraint`` against dirty data fails at migrate time on a live server. The
local database had no violations; that says nothing about staging, so the repair
runs unconditionally and is a no-op where there is nothing to repair.
"""
from django.db import migrations, models
from django.db.models import Count, Max, OuterRef, Subquery
import django.db.models.deletion

TABLE = "vs_schools_branch"


def _flush_deferred_constraints(schema_editor):
    """Drain the deferred FK trigger queue for the branch table.

    Django creates foreign keys as DEFERRABLE INITIALLY DEFERRED, so writing
    rows inside a migration leaves trigger events pending, and PostgreSQL then
    refuses ``ALTER TABLE ... ADD CONSTRAINT`` on that table for the rest of the
    transaction ("cannot ALTER TABLE ... because it has pending trigger
    events"). ``check_constraints`` is Django's own vendor-neutral way to force
    them immediate, so this needs no PostgreSQL branch.
    """
    schema_editor.connection.check_constraints(table_names=[TABLE])


def backfill_branch_tenant(apps, schema_editor):
    """Copy each branch's tenant down from its school."""
    Branch = apps.get_model("vs_schools", "Branch")
    School = apps.get_model("vs_schools", "School")

    updated = Branch.objects.filter(tenant__isnull=True).update(
        tenant_id=Subquery(
            School.objects.filter(pk=OuterRef("school_id")).values("tenant_id")[:1]
        )
    )
    if updated:
        _flush_deferred_constraints(schema_editor)


def dedupe_branch_codes_and_mains(apps, schema_editor):
    """Make the data satisfy the two constraints that are about to be added.

    Duplicate codes: the lowest pk keeps the code, later rows are renumbered
    above the tenant's current maximum. Extra main branches: the lowest pk stays
    main, the rest are demoted. Both orderings are by primary key so the repair
    is deterministic and gives the same answer on every replica.
    """
    Branch = apps.get_model("vs_schools", "Branch")
    repaired = False

    # Materialised up front: the loop rewrites the very rows these group by.
    clashing = list(
        Branch.objects
        .values("tenant_id", "code")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for group in clashing:
        tenant_id = group["tenant_id"]
        next_code = (
            Branch.objects.filter(tenant_id=tenant_id).aggregate(m=Max("code"))["m"] or 0
        )
        losers = list(
            Branch.objects
            .filter(tenant_id=tenant_id, code=group["code"])
            .order_by("pk")
            .values_list("pk", flat=True)
        )[1:]
        for pk in losers:
            next_code += 1
            Branch.objects.filter(pk=pk).update(code=next_code)
            repaired = True

    extra_mains = list(
        Branch.objects
        .filter(is_main=True)
        .values("tenant_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for group in extra_mains:
        demote = list(
            Branch.objects
            .filter(tenant_id=group["tenant_id"], is_main=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )[1:]
        Branch.objects.filter(pk__in=demote).update(is_main=False)
        repaired = True

    if repaired:
        _flush_deferred_constraints(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tenants", "0003_tenantdocumentsequence"),
        ("vs_schools", "0002_alter_branchlifecycle_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="branches",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.RunPython(
            backfill_branch_tenant,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="branch",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="branches",
                to="vs_tenants.tenant",
            ),
        ),
        migrations.RunPython(
            dedupe_branch_codes_and_mains,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                fields=("tenant", "code"), name="uq_branch_tenant_code"
            ),
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_main", True)),
                fields=("tenant",),
                name="uq_branch_one_main_per_tenant",
            ),
        ),
        # help_text only: the code sequence is per tenant now, not per school.
        migrations.AlterField(
            model_name="branch",
            name="code",
            field=models.PositiveIntegerField(
                db_index=True,
                editable=False,
                help_text="Branch code unique per tenant (1..N).",
            ),
        ),
    ]
