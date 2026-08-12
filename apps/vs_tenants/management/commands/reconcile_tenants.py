from django.core.management.base import BaseCommand, CommandError
from django.db.models import F


class Command(BaseCommand):
    """Assert the invariants the tenant refactor is supposed to hold.

    Every check below must be expressed over fields that still exist. Two
    checks here were left behind pointing at ``User.school`` and
    ``LedgerEntity.source_school`` after the refactor dropped both columns,
    which made the whole command raise ``FieldError`` on the first line it
    reached, so none of the surviving invariants were ever verified.
    """

    help = "Verify tenant backfill and cross-tenant invariants before contract rollout."

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant
        from vs_schools.models import Branch, School
        from vs_user.models import User
        from vs_finance.models import LedgerEntity
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

        failures = []
        if Tenant.objects.filter(kind=Tenant.Kind.PLATFORM, slug="codex").count() != 1:
            failures.append("exactly one Codex platform tenant is required")
        if School.objects.filter(tenant__isnull=True).exists():
            failures.append("schools without tenants")
        if User.objects.filter(tenant__isnull=True).exists():
            failures.append("users without tenants")
        # Branch now carries its own tenant, so there are two paths to compare
        # and this is a real invariant. It replaces a vacuous
        # "branches without a school tenant" check that could never fail,
        # School.tenant being non-nullable.
        if Branch.all_objects.filter(tenant__isnull=True).exists():
            failures.append("branches without tenants")
        if Branch.all_objects.exclude(tenant=F("school__tenant")).exists():
            failures.append("branches whose school and tenant disagree")
        if LedgerEntity.objects.filter(tenant__isnull=True).exists():
            failures.append("ledger entities without tenants")
        # ``User.school`` and ``LedgerEntity.source_school`` were the other two
        # legacy links cross-checked here. The refactor dropped both columns, so
        # there is no second path left to disagree with: ``tenant`` is now the
        # only statement of ownership on either model and the null checks above
        # are the whole invariant. Nothing replaces them.
        if TenantRoleTemplate.objects.filter(branch__isnull=False).exclude(tenant=F("branch__school__tenant")).exists():
            failures.append("role templates with cross-tenant branches")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("user__tenant")).exists():
            failures.append("role assignments with cross-tenant users")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("role__tenant")).exists():
            failures.append("role assignments with cross-tenant roles")

        if failures:
            raise CommandError("Tenant reconciliation failed: " + "; ".join(failures))
        self.stdout.write(self.style.SUCCESS("Tenant reconciliation passed."))
