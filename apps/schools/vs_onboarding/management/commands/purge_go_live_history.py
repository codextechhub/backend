"""Delete go-live request history past its one-year retention window.

What it removes: every ``GoLiveRequest`` created more than a year ago, whatever
its status - PENDING requests that went stale unreviewed, REJECTED ones, FAILED
ones, and superseded ACTIVATED ones (activation is idempotent, so approving a
school that is already live marks another row ACTIVATED without changing
anything).

What it keeps, deliberately and regardless of age: the one request per school
that actually took that school live. That row records when the school went live
and who decided it, ``OnboardingProgress.go_live_at`` points at the same moment,
and deleting it destroys the only copy of a fact nothing else can rebuild. A
year does not make it less true.

Idempotent, so it is safe to run on any schedule (the platform's beat runs
``vs_onboarding.purge_go_live_history`` weekly). ``--dry-run`` selects exactly
the same rows and deletes nothing.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Delete go-live requests older than the retention window, keeping the "
        "request that activated each school (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be removed without deleting anything.",
        )

    def handle(self, *args, **options):
        from schools.vs_onboarding.services.retention import purge_go_live_history

        dry_run = options["dry_run"]
        result = purge_go_live_history(dry_run=dry_run)
        prefix = "  [dry-run]" if dry_run else " "

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  Go-live history older than {result['retention_days']} days "
            f"(before {result['cutoff']:%Y-%m-%d %H:%M})\n"
        ))

        if result["by_status"]:
            for status, count in sorted(result["by_status"].items()):
                self.stdout.write(f"{prefix} {status}: {count}")
        else:
            self.stdout.write("  Nothing has aged out of the window.")

        if result["kept_activating"]:
            self.stdout.write(
                f"{prefix} kept {result['kept_activating']} activating "
                f"request(s): the record of a school going live is never purged."
            )

        verb = "would remove" if dry_run else "removed"
        self.stdout.write(self.style.SUCCESS(
            f"\n  Done. {verb} {result['removed']} go-live request(s).\n"
        ))
