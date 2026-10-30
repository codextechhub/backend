"""Delete the personal records past their retention period, now.

The same run the nightly ``core.prune_past_retention`` task makes; see
``core.pruning`` for every period and what is deliberately left alone.
``--dry-run`` counts exactly the rows a real run would delete and deletes
nothing, which is the way to see a first run's size before it happens.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete personal records past their retention period (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Count what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        from core.pruning import SCHEDULE, run

        dry_run = options["dry_run"]
        report = run(dry_run=dry_run)["periods"]
        verb = "would delete" if dry_run else "deleted"
        for period in SCHEDULE:
            entry = report[period.key]
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n  {period.what} (before {entry['cutoff'][:10]})"
            ))
            if "stopped" in entry:
                self.stdout.write(self.style.ERROR(f"  stopped: {entry['stopped']}"))
                continue
            for label, rows in entry["rows"].items():
                label = label.replace("_", " ")
                if isinstance(rows, dict):  # an erasure: how many went, how many failed
                    self.stdout.write(f"  erased {rows['erased']} {label}, {rows['failed']} failed")
                else:
                    self.stdout.write(f"  {verb} {rows} {label}")
        self.stdout.write("")
