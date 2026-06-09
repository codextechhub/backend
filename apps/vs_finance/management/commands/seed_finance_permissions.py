"""Seed vs_finance permission keys and grant them to platform roles (idempotent).

Registers every ``finance.<resource>.<action>`` key enforced by the vs_finance
views into the RBAC Permission registry, tagged with a sensitivity level, and
grants them to the platform admin roles.

Run order::

    python manage.py seed_actions                 # canonical action verbs
    python manage.py create_superuser             # platform roles
    python manage.py seed_finance_permissions

Safe to re-run — all operations are idempotent.
"""
from django.core.management.base import BaseCommand

from vs_rbac.seed_utils import register_app_permissions

PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]

# (resource_name, resource_label, [(action, sensitivity), ...])
# sensitivity: NORMAL (reads / master data) | SENSITIVE (state change) | CRITICAL (money / ledger-irreversible)
FINANCE_RESOURCES = [
    ("entity",       "ledger entities",        [("view", "NORMAL")]),
    ("account",      "chart-of-accounts",      [("view", "NORMAL")]),
    ("costcenter",   "cost centers",           [("view", "NORMAL"), ("create", "NORMAL")]),
    ("dimension",    "reporting dimensions",   [("view", "NORMAL"), ("create", "NORMAL")]),
    ("currency",     "currencies",             [("view", "NORMAL"), ("create", "NORMAL")]),
    ("fxrate",       "FX rates",               [("view", "NORMAL"), ("create", "NORMAL")]),
    ("taxcode",      "tax codes",              [("view", "NORMAL"), ("create", "NORMAL")]),
    ("period",       "accounting periods",     [("view", "NORMAL"), ("close", "CRITICAL")]),
    ("journal",      "journal entries",        [("view", "NORMAL"), ("post", "CRITICAL"), ("reverse", "CRITICAL")]),
    ("invoice",      "customer invoices",      [("view", "NORMAL"), ("writeoff", "SENSITIVE")]),
    ("report",       "financial reports",      [("view", "NORMAL")]),
    ("audit",        "finance audit logs",     [("view", "SENSITIVE")]),
    ("bankaccount",  "bank accounts",          [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("import", "SENSITIVE"), ("reconcile", "SENSITIVE")]),
    ("budget",       "budgets",                [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("edit", "SENSITIVE"), ("approve", "SENSITIVE")]),
    ("concession",   "concessions",            [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "SENSITIVE")]),
    ("creditnote",   "credit/debit notes",     [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("allocate", "SENSITIVE")]),
    ("dunning",      "dunning notices",        [("view", "NORMAL"), ("generate", "SENSITIVE"),
                                                ("send", "SENSITIVE"), ("manage", "SENSITIVE")]),
    ("expenseclaim", "expense claims",         [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("post", "SENSITIVE"), ("settle", "CRITICAL")]),
    ("fixedasset",   "fixed assets",           [("view", "NORMAL"), ("create", "SENSITIVE"),
                                                ("acquire", "SENSITIVE"), ("depreciate", "SENSITIVE")]),
    ("paymentplan",  "payment plans",          [("view", "NORMAL"), ("create", "NORMAL"),
                                                ("activate", "SENSITIVE"), ("cancel", "SENSITIVE")]),
    ("payrollrun",   "payroll runs",           [("view", "SENSITIVE"), ("create", "SENSITIVE"),
                                                ("post", "CRITICAL"), ("pay", "CRITICAL")]),
    ("pettycash",    "petty cash",             [("view", "NORMAL"), ("create", "SENSITIVE"), ("manage", "SENSITIVE"),
                                                ("post", "SENSITIVE"), ("replenish", "SENSITIVE")]),
    ("refund",       "customer refunds",       [("view", "NORMAL"), ("create", "SENSITIVE"), ("post", "CRITICAL")]),
    ("tax",          "tax filings",            [("view", "NORMAL"), ("file", "SENSITIVE"),
                                                ("pay", "CRITICAL"), ("manage", "SENSITIVE")]),
]


class Command(BaseCommand):
    help = "Seed vs_finance permission keys and grant them to platform admin roles."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("\n  Seeding finance permissions...\n"))
        register_app_permissions(
            module_name="finance",
            module_description="General ledger, receivables, banking, payroll, tax and reporting.",
            resources=FINANCE_RESOURCES,
            role_ids=PLATFORM_ROLE_IDS,
            stdout=self.stdout,
            style=self.style,
        )
