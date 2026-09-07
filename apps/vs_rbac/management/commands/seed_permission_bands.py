"""Write onto every permission the capability band that governs it.

This is what makes ``Permission.capability`` real, and so what gives the plan
gate something to read. Until it runs, every key resolves as core and the gate
refuses nothing however it is switched.

Run order::

    python manage.py seed_config_catalogue      # the modules and their bands
    python manage.py seed_all_permissions       # the keys themselves
    python manage.py seed_permission_bands

Safe to re-run, and safe to run before either of the others: a key whose band
capability has not been seeded yet is reported and left alone rather than
guessed at.

Clearing is deliberate too. A key that resolves to no band has its capability
set back to NULL, so moving a line out of the price list takes effect on a
re-run rather than leaving a band nobody meant to keep selling.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from vs_config.models import Capability
from vs_rbac.models import Permission
from vs_rbac.permission_bands import NEVER, NEVER_BAND, band_for, capability_key_for


class Command(BaseCommand):
    help = (
        "Record on each permission the capability band that governs it, so the "
        "plan gate can refuse a key the school's plan does not reach."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report what would be written without writing it.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Banding permissions..." + (" (dry run)" if dry_run else "")
        ))

        capabilities = {
            row.key: row for row in Capability.objects.filter(is_active=True)
        }
        permissions = list(
            Permission.objects.select_related("module", "resource", "action")
        )

        banded = cleared = unchanged = 0
        missing = set()
        counts = {}

        for permission in permissions:
            module = permission.module_id
            resource = permission.resource.name if permission.resource_id else ""
            action = permission.action_id

            wanted_key = capability_key_for(module, resource, action)
            verdict = band_for(module, resource, action)
            counts[verdict] = counts.get(verdict, 0) + 1

            wanted = None
            if wanted_key:
                wanted = capabilities.get(wanted_key)
                if wanted is None:
                    # The band is not in the catalogue yet. Left alone rather
                    # than blanked, so a half-seeded environment does not lose
                    # the mapping it already had.
                    missing.add(wanted_key)
                    continue

            if permission.capability_id == (wanted.pk if wanted else None):
                unchanged += 1
                continue

            if wanted is None:
                cleared += 1
            else:
                banded += 1
            if not dry_run:
                permission.capability = wanted
                permission.save(update_fields=["capability"])

        for label, count in sorted(counts.items(), key=lambda x: str(x[0])):
            name = {None: "core for everybody"}.get(label, str(label).lower())
            self.stdout.write(f"  {count:4d}  {name}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"  {banded} banded, {cleared} cleared, {unchanged} already correct."
        ))
        self.stdout.write(
            f"  {len(NEVER_BAND)} keys are on the never-band list and carry no "
            f"capability by design."
        )
        if missing:
            self.stdout.write(self.style.WARNING(
                "\n  Not in the capability catalogue yet, so their keys were "
                "left as they were:"
            ))
            for key in sorted(missing):
                self.stdout.write(self.style.WARNING(f"    {key}"))
            self.stdout.write(self.style.WARNING(
                "  Run seed_config_catalogue, then run this again."
            ))

        if dry_run:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("\n  Dry run. Nothing written."))
