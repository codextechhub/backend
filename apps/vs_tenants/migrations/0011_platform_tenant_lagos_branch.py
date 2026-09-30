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
by hand). The reverse leaves the branch in place, because by then transactions
name it and removing it would strand them.

Run ``branch_backfill --tenant codex --apply`` after this migration to give the
platform books' existing rows their branch.
"""
from django.db import migrations
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


class Migration(migrations.Migration):

    dependencies = [
        ("vs_tenants", "0010_remove_branch__type"),
    ]

    operations = [
        migrations.RunPython(create_lagos_branch, migrations.RunPython.noop),
    ]
