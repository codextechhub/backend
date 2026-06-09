"""Seed vs_payments permission keys and grant them to platform roles (idempotent).

Registers every ``payments.<resource>.<action>`` key enforced by the
vs_payments views into the RBAC Permission registry and grants them to the
platform admin roles.

Run order::

    python manage.py seed_actions
    python manage.py create_superuser
    python manage.py seed_payments_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand

from vs_rbac.seed_utils import register_app_permissions

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]

# (resource_name, resource_label, [(action, sensitivity), ...])
PAYMENTS_RESOURCES = [
    ("collection",      "gateway collections", [("view", "NORMAL"), ("create", "CRITICAL")]),
    ("payout",          "gateway payouts",     [("view", "NORMAL"), ("create", "CRITICAL")]),
    ("report",          "settlement reports",  [("view", "NORMAL")]),
    ("virtual_account", "virtual accounts",    [("create", "SENSITIVE")]),
]


class Command(BaseCommand):
    help = "Seed vs_payments permission keys and grant them to platform admin roles."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding payments permissions...\n"))
        register_app_permissions(
            module_name="payments",
            module_description="Payment gateway collections, payouts and settlement reconciliation.",
            resources=PAYMENTS_RESOURCES,
            role_ids=PLATFORM_ROLE_IDS,
            stdout=self.stdout,
            style=self.style,
        )
