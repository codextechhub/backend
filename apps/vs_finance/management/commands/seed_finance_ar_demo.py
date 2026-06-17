"""Seed demo Accounts-Receivable data so the AR screens can be driven end-to-end.

Builds on :mod:`vs_finance.seed` (currencies + chart of accounts), then adds a
handful of customers wired to the AR control account and a starter fee structure —
the master data the *New invoice* drawer and *Batch generate* modal need to be
exercised in dev. Idempotent: keyed by ``(entity, code)``, safe to re-run.

Usage::

    manage.py seed_finance_ar_demo                 # platform entity
    manage.py seed_finance_ar_demo --entity CREST  # a specific entity
    manage.py seed_finance_ar_demo --all           # every active entity
"""
from __future__ import annotations

import calendar
import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_finance.constants import PeriodStatus
from vs_finance.models import (
    Account,
    Customer,
    FeeItem,
    FeeStructure,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    TaxCode,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies

DEMO_CUSTOMERS = [
    # @example.com is the RFC-2606 reserved demo domain — well-formed, so the
    # payment gateway accepts it (a bare ".example" TLD is rejected by PSPs).
    ("CUST-001", "Adeyemi & Sons Ltd", "billing.adeyemi@example.com"),
    ("CUST-002", "Brightline Logistics", "brightline.billing@example.com"),
    ("CUST-003", "Crystal Foods Plc", "crystalfoods.finance@example.com"),
    ("CUST-004", "Dunamis Consulting", "dunamis.ar@example.com"),
]

# (line_no, description, revenue_code, amount_kobo, tax_code_or_None)
DEMO_FEE_ITEMS = [
    (1, "Tuition", "4100", 15_000_00, None),
    (2, "Technology levy", "4100", 2_500_00, "VAT"),
    (3, "Library & resources", "4100", 1_000_00, None),
]


class Command(BaseCommand):
    help = "Seed demo customers + a fee structure (and the COA they need) for an entity."

    def add_arguments(self, parser):
        parser.add_argument("--entity", help="LedgerEntity code (defaults to the platform entity).")
        parser.add_argument("--all", action="store_true", help="Seed all active entities.")

    @transaction.atomic
    def handle(self, *args, **options):
        seed_currencies()

        if options["all"]:
            entities = list(LedgerEntity.objects.filter(is_active=True))
        elif options["entity"]:
            entity = LedgerEntity.objects.filter(code=options["entity"]).first()
            if entity is None:
                raise CommandError(f"No LedgerEntity with code '{options['entity']}'.")
            entities = [entity]
        else:
            platform = LedgerEntity.objects.platform()
            if platform is None:
                raise CommandError("No platform entity found; run migrations first.")
            entities = [platform]

        for entity in entities:
            self._seed_entity(entity)

    def _seed_entity(self, entity):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n  {entity.code} — {entity.name}"))

        # Ensure the chart of accounts exists (AR control + revenue + output VAT).
        seed_chart_of_accounts(entity)
        self._ensure_periods(entity)
        ar = Account.objects.filter(entity=entity, code="1200").first()
        revenue = Account.objects.filter(entity=entity, code="4100").first()
        if ar is None or revenue is None:
            self.stdout.write(self.style.WARNING("  ⚠ AR (1200) / revenue (4100) account missing; skipping."))
            return

        tax_by_code = {tc.code: tc for tc in TaxCode.objects.filter(entity=entity)}

        # Customers, each wired to the AR control account.
        new_customers = 0
        for code, name, email in DEMO_CUSTOMERS:
            _, created = Customer.objects.get_or_create(
                entity=entity, code=code,
                defaults={
                    "name": name, "billing_email": email,
                    "receivable_account": ar, "is_active": True,
                },
            )
            new_customers += int(created)
        self.stdout.write(f"  customers: +{new_customers} (of {len(DEMO_CUSTOMERS)})")

        # A starter fee structure with items (drives Batch generate).
        fs, fs_created = FeeStructure.objects.get_or_create(
            entity=entity, code="FS-TERM1",
            defaults={"name": "Term 1 — Standard Fees", "term": "2026/T1", "is_active": True},
        )
        if fs_created or not fs.items.exists():
            for line_no, desc, rev_code, amount, tax_code in DEMO_FEE_ITEMS:
                rev = Account.objects.filter(entity=entity, code=rev_code).first() or revenue
                FeeItem.objects.get_or_create(
                    structure=fs, line_no=line_no,
                    defaults={
                        "description": desc, "revenue_account": rev,
                        "amount": amount, "tax_code": tax_by_code.get(tax_code or ""),
                    },
                )
        self.stdout.write(
            f"  fee structure: {fs.code} ({'created' if fs_created else 'exists'}), {fs.items.count()} item(s)"
        )
        self.stdout.write(self.style.SUCCESS("  ✓ AR demo data ready."))

    def _ensure_periods(self, entity):
        """Open 12 monthly fiscal periods for the current year so invoices can post."""
        year_no = datetime.date.today().year
        fy, _ = FiscalYear.objects.get_or_create(
            entity=entity, year=year_no,
            defaults={
                "start_date": datetime.date(year_no, 1, 1),
                "end_date": datetime.date(year_no, 12, 31),
                "status": PeriodStatus.OPEN,
            },
        )
        opened = 0
        for month in range(1, 13):
            last = calendar.monthrange(year_no, month)[1]
            _, created = FiscalPeriod.objects.get_or_create(
                entity=entity, fiscal_year=fy, period_no=month,
                defaults={
                    "name": datetime.date(year_no, month, 1).strftime("%b %Y"),
                    "start_date": datetime.date(year_no, month, 1),
                    "end_date": datetime.date(year_no, month, last),
                    "status": PeriodStatus.OPEN,
                },
            )
            opened += int(created)
        self.stdout.write(f"  periods: {year_no} fiscal year, +{opened} open month(s)")
