"""Fail a deployment check when an active entity cannot route payout approval.

Usage::

    python manage.py payout_approval_health

The command is read-only. A non-zero exit makes it suitable for a staging smoke test
or deployment gate, and every affected entity is printed before the failure summary.
"""
from django.core.management.base import BaseCommand, CommandError

from vs_payments.approvals import payout_approval_template_gaps


class Command(BaseCommand):
    help = "List active ledger entities that cannot resolve payout-batch approval."

    def handle(self, *args, **options):
        gaps = payout_approval_template_gaps()
        if not gaps:
            self.stdout.write(self.style.SUCCESS(
                "Healthy: every active ledger entity can resolve payout approval.",
            ))
            return

        self.stdout.write(self.style.ERROR(
            f"Unroutable payout approval for {len(gaps)} active ledger "
            f"entit{'y' if len(gaps) == 1 else 'ies'}:",
        ))
        for gap in gaps:
            self.stdout.write(
                f"  tenant={gap['tenant_slug']} "
                f"entity={gap['entity_code']} (id={gap['entity_id']}): "
                f"{gap['reason']}"
            )
        raise CommandError(
            "Payout approval health failed. Publish the platform fallback or seed "
            "the affected tenants before enabling payouts.",
        )
