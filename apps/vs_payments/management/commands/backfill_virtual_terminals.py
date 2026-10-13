"""Give every in-service branch a payment terminal, once (idempotent).

A terminal is opened automatically when a branch is created, so this command is
for the branches that existed before that, and for any whose terminal row was
never written. It skips the platform's own tenant, branches that are out of
service, and branches that already have a live terminal, so it is safe to run
again. A branch whose terminal exists but is still pending or failed is opened
now rather than waiting for the sweep. A branch with no phone number gets a
terminal row that waits (``NEEDS_NUMBER``) until the branch is given a number or
the tenant adds one, and the command says how many.

    python manage.py backfill_virtual_terminals
    python manage.py backfill_virtual_terminals --tenant corona
    python manage.py backfill_virtual_terminals --dry-run
    python manage.py backfill_virtual_terminals --rows-only
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Open a provider payment terminal for every in-service branch that has none."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant (its slug).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would be created without writing or calling the provider.")
        parser.add_argument("--rows-only", action="store_true",
                            help="Write the terminal rows and leave opening them to the sweep.")

    def handle(self, *args, **options):
        from vs_payments.terminals import backfill

        summary = backfill(
            tenant_slug=options.get("tenant"), dry_run=options["dry_run"],
            open_now=not options["rows_only"])
        verb = "would create" if options["dry_run"] else "created"
        self.stdout.write(
            f"Terminals {verb}: {summary['created']}; already present: {summary['existing']}; "
            f"opened at the provider: {summary['opened']}; waiting for a branch phone number: "
            f"{summary['needs_number']}; not opened: {summary['failed']}.")
