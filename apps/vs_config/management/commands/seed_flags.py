# vs_config/management/commands/seed_flags.py
#
# Management command: seed_flags
#
# Seeds BranchFeatureFlag records for all branches against the
# current FLAG_REGISTRY. Safe to run multiple times — uses get_or_create
# so existing records are never overwritten.
#
# Typical use cases:
#   - After adding a new flag to FLAG_REGISTRY, run this to create
#     disabled records for all existing branches so the flag
#     appears in their flag panel immediately.
#   - During initial platform setup to seed flags for all existing branches.
#   - In CI or staging resets to restore a clean flag state.
#
# Usage:
#   python manage.py seed_flags
#   python manage.py seed_flags --branch <slug>    (single branch)
#   python manage.py seed_flags --dry-run          (preview, no writes)
#   python manage.py seed_flags --reset            (delete all flags first, then reseed)
#
# WARNING: --reset deletes ALL existing BranchFeatureFlag records for
# the targeted branches and replaces them with all-disabled defaults.
# Use with caution in production — this will disable any flags that were
# previously enabled.

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_config.constants import FLAG_REGISTRY
from vs_config.models import BranchFeatureFlag


class Command(BaseCommand):
    help = (
        "Seed BranchFeatureFlag records for all branches against the "
        "current FLAG_REGISTRY. Idempotent by default. Use --reset to wipe and "
        "reseed (WARNING: disables all previously enabled flags)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--branch",
            type=str,
            default=None,
            help="Slug of a single branch to seed. Omit to seed all branches.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Preview what would be created without writing to the database.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            default=False,
            help=(
                "Delete all existing flag records for the target branch(es) "
                "before reseeding. This disables all previously enabled flags. "
                "Requires confirmation prompt unless --no-input is also passed."
            ),
        )
        parser.add_argument(
            "--no-input",
            action="store_true",
            default=False,
            help="Skip the --reset confirmation prompt. Use in CI/scripts.",
        )

    def handle(self, *args, **options):
        from vs_schools.models import Branch

        dry_run    = options["dry_run"]
        reset      = options["reset"]
        no_input   = options["no_input"]
        branch_slug = options["branch"]

        # ------------------------------------------------------------------
        # Resolve target branches
        # ------------------------------------------------------------------
        if branch_slug:
            try:
                branches = [Branch.objects.get(slug=branch_slug)]
            except Branch.DoesNotExist:
                raise CommandError(
                    f"Branch with slug '{branch_slug}' not found."
                )
        else:
            branches = list(Branch.objects.all())

        if not branches:
            self.stdout.write(self.style.WARNING("No branches found. Nothing to seed."))
            return

        self.stdout.write(
            f"\nTarget: {len(branches)} branch(es) | "
            f"Flags in registry: {len(FLAG_REGISTRY)} | "
            f"Mode: {'DRY RUN' if dry_run else ('RESET + SEED' if reset else 'SEED (idempotent)')}\n"
        )

        # ------------------------------------------------------------------
        # Confirm reset if requested
        # ------------------------------------------------------------------
        if reset and not dry_run and not no_input:
            self.stdout.write(
                self.style.WARNING(
                    "\nWARNING: --reset will DELETE all existing flag records for "
                    f"the {len(branches)} targeted branch(es) and replace them "
                    "with all-disabled defaults. Any previously enabled flags will be "
                    "turned OFF.\n"
                )
            )
            confirm = input("Type 'yes' to continue, or anything else to abort: ")
            if confirm.strip().lower() != "yes":
                self.stdout.write(self.style.ERROR("Aborted."))
                return

        # ------------------------------------------------------------------
        # Execute
        # ------------------------------------------------------------------
        total_created = 0
        total_skipped = 0
        total_deleted = 0

        for branch in branches:
            self.stdout.write(f"  Processing: {branch.name} ({branch.slug})")

            with transaction.atomic():
                if reset and not dry_run:
                    deleted_count, _ = BranchFeatureFlag.objects.filter(
                        branch=branch
                    ).delete()
                    total_deleted += deleted_count
                    self.stdout.write(
                        f"    Deleted {deleted_count} existing flag record(s)."
                    )

                for flag_key, label in FLAG_REGISTRY.items():
                    if dry_run:
                        exists = BranchFeatureFlag.objects.filter(
                            branch=branch,
                            flag_key=flag_key,
                        ).exists()
                        if exists and not reset:
                            self.stdout.write(
                                f"    [DRY RUN] SKIP  {flag_key} (already exists)"
                            )
                            total_skipped += 1
                        else:
                            self.stdout.write(
                                f"    [DRY RUN] CREATE {flag_key} — {label}"
                            )
                            total_created += 1
                    else:
                        _, created = BranchFeatureFlag.objects.get_or_create(
                            branch=branch,
                            flag_key=flag_key,
                            defaults={"is_enabled": False, "set_by": None},
                        )
                        if created:
                            total_created += 1
                        else:
                            total_skipped += 1

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[DRY RUN] Would create {total_created} record(s), "
                    f"skip {total_skipped} existing record(s). No changes written."
                )
            )
        else:
            if total_deleted:
                self.stdout.write(
                    self.style.WARNING(f"Deleted {total_deleted} existing flag record(s).")
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Created {total_created} record(s), "
                    f"skipped {total_skipped} existing record(s)."
                )
            )
