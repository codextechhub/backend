"""Give every tenant with books the approval routes provisioning gives a new one.

Books provisioning publishes a tenant's own procurement routes when its first
set of books is created (:func:`vs_procurement.provisioning.provision_approval_ladders`).
A route that provisioning starts publishing later reaches tenants created before
it only through this command, which runs the same function over every tenant
that keeps books. A school whose requisition route is still empty gains the
line manager step; every route somebody has configured is left exactly as it is.

Runs on every deploy (``build.sh``). Safe to re-run; ``--dry-run`` reports and
writes nothing.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Publish each tenant's own procurement approval routes, as books provisioning does."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")

    def handle(self, *args, **options):
        from vs_finance.models import LedgerEntity

        from vs_procurement.approvals import ensure_tenant_approval_templates

        tenants = {
            entity.tenant_id: entity.tenant
            for entity in LedgerEntity.objects.filter(tenant__isnull=False).select_related("tenant")
        }
        with transaction.atomic():
            for tenant in sorted(tenants.values(), key=lambda t: t.slug):
                published = [
                    template for template, created
                    in ensure_tenant_approval_templates(tenant, with_default_stages=False)
                    if created
                ]
                if published:
                    names = ", ".join(template.name for template in published)
                    self.stdout.write(f"  {tenant.slug}: published {names}")
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
                return
        self.stdout.write(self.style.SUCCESS("Procurement approval routes are in place."))
