from django.core.management.base import BaseCommand, CommandError
from django.db.models import F


class Command(BaseCommand):
    """Assert the invariants tenant ownership is supposed to hold.

    Two rules govern what may be checked here. Every check must be expressed
    over fields that still exist, because one stale field name raises
    ``FieldError`` on the line it reaches and leaves every surviving invariant
    unverified. And every check must be able to fail: a query over a
    non-nullable relationship can never match a row, so it asserts nothing
    while looking like it does.

    Ownership is stated once, by ``tenant``, and there is no second path to
    cross-check it against. The null checks are therefore the whole invariant
    for users, branches and ledger entities; the ``F`` comparisons cover the
    rows that carry two tenant-bearing links at once and so can disagree with
    themselves.

    Nothing here may import from the schools product: this is a platform
    command and the models it verifies are platform models.
    """

    help = "Verify tenant backfill and cross-tenant invariants before contract rollout."

    def handle(self, *args, **options):
        from vs_tenants.models import Branch, Tenant
        from vs_user.models import User
        from vs_finance.models import LedgerEntity
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

        failures = []
        if Tenant.objects.filter(kind=Tenant.Kind.PLATFORM, slug="codex").count() != 1:
            failures.append("exactly one Codex platform tenant is required")
        if User.objects.filter(tenant__isnull=True).exists():
            failures.append("users without tenants")
        # NOT NULL in the schema; fires only if a migration left rows behind.
        if Branch.all_objects.filter(tenant__isnull=True).exists():
            failures.append("branches without tenants")
        if LedgerEntity.objects.filter(tenant__isnull=True).exists():
            failures.append("ledger entities without tenants")
        if TenantRoleTemplate.objects.filter(branch__isnull=False).exclude(tenant=F("branch__tenant")).exists():
            failures.append("role templates with cross-tenant branches")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("user__tenant")).exists():
            failures.append("role assignments with cross-tenant users")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("role__tenant")).exists():
            failures.append("role assignments with cross-tenant roles")

        if failures:
            raise CommandError("Tenant reconciliation failed: " + "; ".join(failures))
        self.stdout.write(self.style.SUCCESS("Tenant reconciliation passed."))
