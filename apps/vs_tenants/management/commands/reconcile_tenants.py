from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q


class Command(BaseCommand):
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
        if User.objects.filter(school__isnull=False).exclude(tenant=F("school__tenant")).exists():
            failures.append("users whose legacy school and tenant disagree")
        if Branch.all_objects.exclude(school__tenant__isnull=False).exists():
            failures.append("branches without a school tenant")
        if LedgerEntity.objects.filter(tenant__isnull=True).exists():
            failures.append("ledger entities without tenants")
        if LedgerEntity.objects.filter(source_school__isnull=False).exclude(tenant=F("source_school__tenant")).exists():
            failures.append("ledger entities whose source school and tenant disagree")
        if TenantRoleTemplate.objects.filter(branch__isnull=False).exclude(tenant=F("branch__school__tenant")).exists():
            failures.append("role templates with cross-tenant branches")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("user__tenant")).exists():
            failures.append("role assignments with cross-tenant users")
        if TenantUserRoleAssignment.objects.exclude(tenant=F("role__tenant")).exists():
            failures.append("role assignments with cross-tenant roles")

        if failures:
            raise CommandError("Tenant reconciliation failed: " + "; ".join(failures))
        self.stdout.write(self.style.SUCCESS("Tenant reconciliation passed."))
