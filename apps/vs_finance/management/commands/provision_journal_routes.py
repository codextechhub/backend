"""Give every tenant with books the hand-journal approval route provisioning gives a new one.

Books provisioning publishes a tenant's own hand-journal route, with no steps, when
its first set of books is created
(:func:`vs_finance.provisioning_hooks.provision_adjustment_approvals`). A tenant whose
books were created before that route existed gains it only through this command, which
runs the same function over every tenant that keeps books. A route somebody has
configured is left exactly as it is.

Only the journal route is published. The other finance routes are not backfilled here,
because publishing them would change how refunds, write-offs and the rest behave for
tenants that never had a route for them.

Runs on every deploy (``build.sh``). Safe to re-run; ``--dry-run`` reports and writes
nothing.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Publish each tenant's own hand-journal approval route, as books provisioning does."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")

    def handle(self, *args, **options):
        from vs_finance.approvals import ensure_tenant_journal_template
        from vs_finance.models import LedgerEntity

        tenants = {
            entity.tenant_id: entity.tenant
            for entity in LedgerEntity.objects.filter(tenant__isnull=False).select_related("tenant")
        }
        with transaction.atomic():
            for tenant in sorted(tenants.values(), key=lambda t: t.slug):
                _template, created = ensure_tenant_journal_template(tenant)
                if created:
                    self.stdout.write(f"  {tenant.slug}: published the journal entry route")
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
                return
        self.stdout.write(self.style.SUCCESS("Hand-journal approval routes are in place."))
