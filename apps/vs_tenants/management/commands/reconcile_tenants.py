from django.core.management.base import BaseCommand, CommandError
from django.db.models import F


class Command(BaseCommand):
    """Assert the invariants the tenant refactor is supposed to hold.

    Every check below must be expressed over fields that still exist. Two
    checks here were left behind pointing at ``User.school`` and
    ``LedgerEntity.source_school`` after the refactor dropped both columns,
    which made the whole command raise ``FieldError`` on the first line it
    reached, so none of the surviving invariants were ever verified.

    A check must also be able to fail. "Schools without tenants" could not:
    ``School.tenant`` is a non-nullable OneToOneField, so the query it ran can
    never match a row. It was the only thing this platform command imported
    from the schools product, and it went with the branch move.
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
        # ``tenant`` is the only statement of a branch's ownership now that the
        # ``school`` column is gone, so the null check is the whole invariant.
        # The companion check that compared it against ``school__tenant`` went
        # with the column: with one path there is nothing left to disagree.
        # The column itself is NOT NULL, so this can only fire if a migration
        # left rows behind; it is cheap and it is the one that would matter.
        if Branch.all_objects.filter(tenant__isnull=True).exists():
            failures.append("branches without tenants")
        if LedgerEntity.objects.filter(tenant__isnull=True).exists():
            failures.append("ledger entities without tenants")
        # ``User.school`` and ``LedgerEntity.source_school`` were the other two
        # legacy links cross-checked here. The refactor dropped both columns, so
        # there is no second path left to disagree with: ``tenant`` is now the
        # only statement of ownership on either model and the null checks above
        # are the whole invariant. Nothing replaces them.
        if TenantRoleTemplate.objects.filter(branch__isnull=False).exclude(tenant=F("branch__tenant")).exists():
            failures.append("role templates with cross-tenant branches")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("user__tenant")).exists():
            failures.append("role assignments with cross-tenant users")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("role__tenant")).exists():
            failures.append("role assignments with cross-tenant roles")

        if failures:
            raise CommandError("Tenant reconciliation failed: " + "; ".join(failures))
        self.stdout.write(self.style.SUCCESS("Tenant reconciliation passed."))
