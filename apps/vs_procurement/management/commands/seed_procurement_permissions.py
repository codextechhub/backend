"""Seed vs_procurement permission keys and grant them to platform roles (idempotent).

Registers every ``procurement.<resource>.<action>`` key enforced by the
vs_procurement views into the RBAC Permission registry and grants them to the
platform admin roles.

Run order::

    python manage.py seed_actions
    python manage.py create_superuser
    python manage.py seed_procurement_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand

from vs_rbac.seed_utils import register_app_permissions

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]

# (resource_name, resource_label, [(action, sensitivity), ...])
PROCUREMENT_RESOURCES = [
    ("approval",       "spend approvals",       [("approve", "SENSITIVE"), ("approve_senior", "CRITICAL"), ("manage", "SENSITIVE")]),
    ("catalog_item",   "catalog items",         [("view", "NORMAL"), ("create", "NORMAL"), ("update", "NORMAL")]),
    ("category",       "vendor categories",     [("view", "NORMAL"), ("create", "NORMAL")]),
    ("contract",       "vendor contracts",      [("view", "NORMAL"), ("create", "SENSITIVE"), ("update", "SENSITIVE"),
                                                 ("activate", "SENSITIVE"), ("renew", "SENSITIVE"), ("terminate", "SENSITIVE")]),
    ("goods_receipt",  "goods-received notes",  [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "CRITICAL")]),
    ("purchase_order", "purchase orders",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("submit", "SENSITIVE")]),
    ("quotation",      "vendor quotations",     [("view", "NORMAL"), ("create", "NORMAL"), ("submit", "SENSITIVE"), ("award", "SENSITIVE")]),
    ("report",         "procurement reports",   [("view", "NORMAL")]),
    ("requisition",    "purchase requisitions", [("view", "NORMAL"), ("create", "NORMAL"), ("submit", "SENSITIVE")]),
    ("rfq",            "requests for quotation", [("view", "NORMAL"), ("create", "NORMAL"), ("issue", "SENSITIVE")]),
    ("stock",          "stock items",           [("view", "NORMAL"), ("manage", "SENSITIVE"), ("issue", "SENSITIVE"), ("adjust", "SENSITIVE")]),
    ("vendor",         "vendors",               [("view", "NORMAL"), ("create", "SENSITIVE")]),
    ("vendor_invoice", "vendor invoices",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("submit", "SENSITIVE"),
                                                 ("match", "SENSITIVE"), ("post", "CRITICAL")]),
    ("vendor_payment", "vendor payments",       [("view", "NORMAL"), ("create", "CRITICAL"), ("post", "CRITICAL")]),
]


class Command(BaseCommand):
    help = "Seed vs_procurement permission keys and grant them to platform admin roles."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding procurement permissions...\n"))
        register_app_permissions(
            module_name="procurement",
            module_description="Procure-to-pay: requisitions, sourcing, receipts, vendor invoicing and payments.",
            resources=PROCUREMENT_RESOURCES,
            role_ids=PLATFORM_ROLE_IDS,
            stdout=self.stdout,
            style=self.style,
        )
