"""Give every school its leave approval ladder.

A school's leave ladder is published when the school is created and again at
its first leave filing. A school that predates the first and has not yet seen
the second has none, and files nothing until it does. This command runs the same
function over every school so a deployed database is repaired in one pass instead
of one refusal at a time.

Non-destructive and idempotent: a school that already has a leave ladder, of
either shape, is left exactly as it is. ``--dry-run`` reports and writes nothing,
and ``--tenant`` narrows the run to one school's slug.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Publish the leave approval ladder for every school that does not have one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", help="Only the school with this slug.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be created; write nothing.",
        )

    def handle(self, *args, **options):
        from vs_tenants.models import Tenant

        from ...approvals import ensure_tenant_approval_templates

        schools = Tenant.objects.filter(kind=Tenant.Kind.SCHOOL).order_by("slug")
        if options["tenant"]:
            schools = schools.filter(slug=options["tenant"])
            if not schools.exists():
                raise CommandError(f"No school with the slug {options['tenant']!r}.")

        created, kept = [], 0
        with transaction.atomic():
            for tenant in schools:
                _template, was_created = ensure_tenant_approval_templates(tenant)
                if was_created:
                    created.append(tenant.slug)
                    self.stdout.write(f"  {tenant.slug}: published the leave ladder")
                else:
                    kept += 1
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
        self.stdout.write(self.style.SUCCESS(
            f"Leave ladder: {len(created)} published, {kept} already in place."
        ))
