"""Send platform operators the fortnightly list of onboardings going stale.

Two lists in one message: schools that have been onboarding longer than the
stale threshold and have not yet expired, and schools the 90-day sweep expired
inside the last reporting window. The second is read from the audit trail
rather than from tenant status, so a school suspended by hand for some other
reason never appears here.

The audience is resolved the way every other audience in this module is,
through ``vs_rbac.evaluator.resolve_users_with_permission``, from a key
platform staff already hold (``onboarding.go_live.approve``). In-app and email
only.

Silent when there is nothing to say. ``--dry-run`` assembles and prints the
list without dispatching anything.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Assemble the stale-onboarding list and notify platform operators "
        "(idempotent; safe to re-run)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the list without sending any notification.",
        )

    def handle(self, *args, **options):
        from schools.vs_onboarding.services.lifecycle import stale_onboarding_report

        dry_run = options["dry_run"]
        result = stale_onboarding_report(dispatch=not dry_run)
        context = result["context"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n  Onboarding pending longer than {context['stale_after_days']} "
            f"days, and expiries in the last {context['window_days']} days\n"
        ))
        self.stdout.write(f"  Ageing ({context['ageing_count']}):")
        self.stdout.write(f"{context['ageing_list']}\n")
        self.stdout.write(f"  Recently expired ({context['expired_count']}):")
        self.stdout.write(f"{context['expired_list']}\n")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "\n  [dry-run] Nothing was dispatched.\n"
            ))
        elif result["dispatched"]:
            self.stdout.write(self.style.SUCCESS(
                f"\n  Done. Reported to {result['recipients']} platform "
                f"operator(s).\n"
            ))
        elif not result["recipients"]:
            self.stdout.write(self.style.WARNING(
                "\n  !  No platform operator holds onboarding.go_live.approve, "
                "so there was nobody to tell. Run seed_onboarding_permissions.\n"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\n  Done. Nothing is going stale, so no report was sent.\n"
            ))
