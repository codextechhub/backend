"""Give the platform tenant its branch: Lagos.

Every transaction names a branch, and every tenant keeps at least one. CodeX's
own books (the platform tenant, codex) belonged to a tenant that owned no
branch at all, so its invoices, journals and purchases could not be given one:
the branch backfill reported those books as blocked and year close and VAT
filing had no branch to split by. The owner named the platform's branch after
its city.

The branch is CodeX's main branch, active from the moment it exists, recorded
the way ``Branch.transition`` records an activation (a PENDING to ACTIVE
lifecycle event with no actor, as for any system-driven change). Code 1, since
the tenant owns no other branch.

Idempotent: nothing happens where codex is absent (a database built without
the platform seed) or already owns a branch (a database where one was created
by hand).

It depends on vs_schools 0004, the migration that let a branch exist outside a
school: codex is not a school, and before 0004 every branch needed one. The
reverse removes Lagos only while nothing names it (a freshly built database, or
a rewind past 0004), and otherwise leaves it, because transactions naming it
would be stranded.

Run ``branch_backfill --tenant codex --apply`` after this migration to give the
platform books' existing rows their branch.
"""
from django.db import IntegrityError, migrations, transaction
from django.utils import timezone

CODEX_SLUG = "codex"
BRANCH_NAME = "Lagos"


def create_lagos_branch(apps, schema_editor):
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Branch = apps.get_model("vs_tenants", "Branch")
    BranchLifecycle = apps.get_model("vs_tenants", "BranchLifecycle")

    codex = Tenant.objects.filter(slug=CODEX_SLUG, kind="PLATFORM").first()
    if codex is None or Branch.objects.filter(tenant=codex).exists():
        return

    now = timezone.now()
    branch = Branch.objects.create(
        tenant=codex,
        name=BRANCH_NAME,
        code=1,
        is_main=True,
        country="Nigeria",
        state="Lagos",
        status="ACTIVE",
        activated_at=now,
        created_at=now,
    )
    BranchLifecycle.objects.create(
        branch=branch,
        from_state="PENDING",
        to_state="ACTIVE",
        actor_id="",
        reason="The platform's books need a branch: every transaction names one.",
        occurred_at=now,
    )


def remove_unused_lagos_branch(apps, schema_editor):
    """Remove Lagos while nothing names it; leave it where anything does.

    The database decides, not the migration state: mid-rewind that state lists
    relations whose tables or columns an earlier step has already dropped. The
    delete runs in a savepoint and the foreign keys are checked at once; any row
    still pointing at the branch fails the check and the savepoint rolls back.
    """
    Tenant = apps.get_model("vs_tenants", "Tenant")
    Branch = apps.get_model("vs_tenants", "Branch")
    BranchLifecycle = apps.get_model("vs_tenants", "BranchLifecycle")

    codex = Tenant.objects.filter(slug=CODEX_SLUG, kind="PLATFORM").first()
    if codex is None:
        return
    connection = schema_editor.connection
    branch_table = connection.ops.quote_name(Branch._meta.db_table)
    lifecycle_table = connection.ops.quote_name(BranchLifecycle._meta.db_table)
    branch_ids = list(
        Branch.objects.filter(tenant=codex, name=BRANCH_NAME).values_list("pk", flat=True)
    )
    for branch_id in branch_ids:
        try:
            with transaction.atomic(using=connection.alias):
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {lifecycle_table} WHERE branch_id = %s", [branch_id])
                    cursor.execute(f"DELETE FROM {branch_table} WHERE id = %s", [branch_id])
                connection.check_constraints()
        except IntegrityError:
            continue


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tenants", "0010_remove_branch__type"),
        ("vs_schools", "0004_branch_drop_school"),
    ]

    operations = [
        migrations.RunPython(create_lagos_branch, remove_unused_lagos_branch),
    ]
