"""Seed a school's books with the spending the Cash, spend & compliance tab reads.

A development aid, run after ``seed_finance_dashboard_demo`` on the same books
(it pays from that command's bank accounts). It adds cost centres and the money
going out through them: last month's salaries paid early in this month and their
PAYE remitted, bills paid straight from the bank, staff expense claims at every
stage, and a fixed-asset register.

The register is brought in the way one migrated from another system is: each
asset at its original cost, with the depreciation charged before these books
opened carried in as a single brought-forward entry, and this month's charge
posted through the normal depreciation service.

Everything is posted through the finance services where one exists, dated in
the open period, and marked ``DEMO``. Refuses to run twice on the same books, and
on production settings.

Usage::

    manage.py seed_finance_spend_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

MARK = "DEMO"


class Command(BaseCommand):
    help = "Seed demo spending (cost centres, payroll paid, claims, assets) on one school's books."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import BankAccount, CostCenter, LedgerEntity

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if CostCenter.objects.filter(entity=entity, code__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the spending demo data.")
        if not BankAccount.objects.filter(entity=entity, name__startswith=MARK).exists():
            raise CommandError("Run seed_finance_dashboard_demo on these books first.")
        with transaction.atomic():
            Seeder(entity, datetime.date.today(), random.Random(f"{entity.code}-spend"), self.stdout).run()


class Seeder:
    def __init__(self, entity, as_of, rng, out):
        from django.contrib.auth import get_user_model

        from vs_finance.models import FiscalPeriod

        self.entity, self.as_of, self.rng, self.out = entity, as_of, rng, out
        period = FiscalPeriod.objects.filter(
            entity=entity, status="OPEN", start_date__lte=as_of, end_date__gte=as_of,
        ).first()
        if period is None:
            raise CommandError(f"{entity.code} has no open period containing {as_of}.")
        self.start = period.start_date
        self.actor = get_user_model().objects.filter(tenant=entity.tenant, is_active=True).order_by("id").first()

    def say(self, text):
        self.out.write(f"  {text}")

    def day(self, n):
        return min(self.start + datetime.timedelta(days=n), self.as_of)

    def account(self, code):
        from vs_finance.models import Account

        return Account.objects.get(entity=self.entity, code=code)

    def bank(self, word):
        from vs_finance.models import BankAccount

        return BankAccount.objects.get(entity=self.entity, name__startswith=MARK, name__icontains=word)

    def run(self):
        self.out.write(f"Seeding spending demo data on {self.entity.code}:")
        self.centres = self.cost_centres()
        self.salaries_paid()
        self.bills_paid()
        self.claims()
        self.assets()
        self.out.write("Done.")

    def cost_centres(self):
        from vs_finance.models import CostCenter

        names = {"TEACH": "Teaching staff", "ADMIN": "Administration", "FAC": "Facilities",
                 "TRANS": "Transport", "FEED": "Feeding"}
        centres = {
            key: CostCenter.objects.create(entity=self.entity, code=f"{MARK}-{key}", name=name)
            for key, name in names.items()
        }
        self.say(f"{len(centres)} cost centres")
        return centres

    def salaries_paid(self):
        """Last month's salaries, paid in the first days of this month, and their PAYE remitted."""
        from vs_finance.models import PayrollLine, PayrollRun, TaxFiling, TaxObligation
        from vs_finance.payroll import compute_payroll, pay_payroll, post_payroll
        from vs_finance.tax_filing import pay_filing

        last_month = self.start - datetime.timedelta(days=1)
        run = PayrollRun.objects.create(
            entity=self.entity, pay_date=self.day(1), period_label=f"{last_month:%B}",
            narration=f"{MARK} {last_month:%B} salaries", bank_account=self.bank("payroll"),
        )
        for i in range(1, 25):
            gross = self.rng.choice([180_000_00, 220_000_00, 260_000_00, 320_000_00, 450_000_00])
            PayrollLine.objects.create(
                run=run, line_no=i, employee_name=f"Staff member {i}", gross_amount=gross,
                paye_amount=gross * 11 // 100, pension_amount=gross * 8 // 100,
                cost_center=self.centres["TEACH" if i <= 18 else "ADMIN"],
            )
        compute_payroll(run)
        run.refresh_from_db()
        post_payroll(run, actor_user=self.actor)
        pay_payroll(run, bank_account=self.bank("payroll"), pay_date=self.day(1), actor_user=self.actor)

        paye = TaxObligation.objects.filter(entity=self.entity, code="PAYE").first()
        if paye is not None:
            filing = TaxFiling.objects.create(
                entity=self.entity, obligation=paye, period_start=last_month.replace(day=1),
                period_end=last_month, due_date=self.start + datetime.timedelta(days=9),
                filing_status="FILED", filed_at=self.day(7), filing_reference=f"{MARK}-PAYE-{last_month:%m}",
                gross_liability=run.paye_total, amount_due=run.paye_total,
            )
            pay_filing(filing, bank_account=self.bank("operations"), pay_date=self.day(8), actor_user=self.actor)
        self.say(f"{run.period_label} salaries paid on {run.pay_date} and PAYE remitted")

    def bills_paid(self):
        """Running costs paid straight from the operations account, each to its cost centre."""
        from vs_finance.constants import JournalSource
        from vs_finance.models import FiscalPeriod, JournalEntry, JournalLine
        from vs_finance.posting import post_journal

        bank = self.bank("operations").gl_account
        specs = [
            ("FAC", "Electricity and generator diesel", 1_240_000_00, 3),
            ("TRANS", "Diesel and servicing for the school buses", 980_000_00, 5),
            ("FEED", "Kitchen provisions", 1_460_000_00, 6),
            ("FAC", "Roof repair on the science block", 720_000_00, 10),
            ("TRANS", "Bus insurance renewal", 410_000_00, 12),
            ("FEED", "Kitchen provisions", 1_310_000_00, 17),
            ("ADMIN", "Internet and phone lines", 186_000_00, 18),
            ("FAC", "Cleaning contract", 350_000_00, 20),
            (None, "Bank charges", 42_500_00, 24),
        ]
        for n, (centre, narration, amount, offset) in enumerate(specs, start=1):
            date = self.day(offset)
            entry = JournalEntry.objects.create(
                entity=self.entity, date=date, source=JournalSource.BANK,
                period=FiscalPeriod.objects.get(entity=self.entity, start_date__lte=date, end_date__gte=date),
                narration=f"{MARK} {narration}", reference=f"{MARK}-SPEND-{n}",
            )
            JournalLine.objects.create(
                entry=entry, line_no=1, account=self.account("5500" if centre is None else "5300"),
                debit=amount, credit=0, description=narration,
                cost_center=self.centres[centre] if centre else None,
            )
            JournalLine.objects.create(entry=entry, line_no=2, account=bank, debit=0, credit=amount,
                                       description=narration)
            post_journal(entry, actor_user=self.actor)
        self.say(f"{len(specs)} bills paid from the operations account")

    def claims(self):
        """Staff expense claims: waiting for approval, approved and owed, and reimbursed."""
        from vs_finance.expenses import post_expense_claim, price_expense_claim, settle_expense_claim
        from vs_finance.models import ExpenseClaim, ExpenseClaimLine

        specs = [
            ("Mr Adebayo", "Inter-house sports trip", "TRANS", 84_000_00, 2, "waiting"),
            ("Mrs Chukwu", "Lab reagents", "FAC", 56_500_00, 5, "waiting"),
            ("Mr Lawal", "Generator repair", "FAC", 112_000_00, 8, "waiting"),
            ("Mrs Bello", "Printing for mid-term tests", "ADMIN", 38_000_00, 9, "owed"),
            ("Mr Okafor", "Kitchen gas refill", "FEED", 64_000_00, 11, "owed"),
            ("Mrs Eze", "Teaching aids", "TEACH", 27_500_00, 3, "paid"),
            ("Mr Musa", "Bus tyre replacement", "TRANS", 146_000_00, 4, "paid"),
            ("Mrs Adeyemi", "Staff room supplies", "ADMIN", 22_000_00, 6, "paid"),
        ]
        for n, (who, title, centre, amount, offset, state) in enumerate(specs, start=1):
            claim = ExpenseClaim.objects.create(
                entity=self.entity, claimant_name=who, claim_date=self.day(offset),
                title=title, narration=f"{MARK} {title}",
            )
            ExpenseClaimLine.objects.create(
                claim=claim, line_no=1, description=title, expense_account=self.account("5300"),
                quantity=1, unit_price=amount, cost_center=self.centres[centre],
            )
            price_expense_claim(claim)
            if state == "waiting":
                claim.status = "PENDING_APPROVAL"
                claim.save(update_fields=["status", "updated_at"])
                continue
            post_expense_claim(claim, actor_user=self.actor)
            if state == "paid":
                settle_expense_claim(claim, bank_account=self.bank("operations"),
                                     pay_date=self.day(offset + 4), actor_user=self.actor)
        self.say(f"{len(specs)} expense claims: 3 waiting, 2 approved and owed, 3 reimbursed")

    def assets(self):
        """A register brought forward at cost, with depreciation to date carried in."""
        from vs_finance.assets import acquire_asset, build_depreciation_schedule, post_depreciation
        from vs_finance.models import FiscalPeriod, FixedAsset, JournalEntry, JournalLine
        from vs_finance.posting import post_journal

        specs = [
            ("Main block and classrooms", "BUILDINGS", 180_000_000_00, datetime.date(2015, 1, 15), 600),
            ("Annex hostel", "BUILDINGS", 42_000_000_00, datetime.date(2019, 9, 15), 480),
            ("School bus 1", "VEHICLES", 18_000_000_00, datetime.date(2017, 1, 15), 60),
            ("School bus 2", "VEHICLES", 18_000_000_00, datetime.date(2017, 1, 15), 60),
            ("School bus 3", "VEHICLES", 18_500_000_00, datetime.date(2018, 3, 15), 60),
            ("Coaster bus", "VEHICLES", 32_000_000_00, datetime.date(2023, 5, 15), 96),
            ("Classroom furniture", "FURNITURE", 14_600_000_00, datetime.date(2021, 9, 15), 120),
            ("Computer lab", "IT_EQUIPMENT", 12_800_000_00, datetime.date(2024, 1, 15), 48),
            ("Science lab equipment", "EQUIPMENT", 6_400_000_00, datetime.date(2022, 9, 15), 84),
        ]
        brought_forward = 0
        assets = []
        for name, category, cost, bought, months in specs:
            asset = FixedAsset.objects.create(
                entity=self.entity, name=f"{MARK} {name}", category=category, acquisition_date=self.start,
                cost=cost, salvage_value=0, useful_life_months=months,
            )
            acquire_asset(asset, credit_account=self.account("3200"), actor_user=self.actor, build_schedule=False)
            asset.acquisition_date = bought
            asset.save(update_fields=["acquisition_date", "updated_at"])
            build_depreciation_schedule(asset)
            before = asset.schedule.filter(depreciation_date__lt=self.start)
            carried = sum(before.values_list("amount", flat=True))
            before.update(is_posted=True)
            asset.accumulated_depreciation = carried
            if not asset.schedule.filter(is_posted=False).exists():
                asset.asset_status = "FULLY_DEPRECIATED"
            asset.save(update_fields=["accumulated_depreciation", "asset_status", "updated_at"])
            brought_forward += carried
            assets.append(asset)

        entry = JournalEntry.objects.create(
            entity=self.entity, date=self.start,
            period=FiscalPeriod.objects.get(entity=self.entity, start_date__lte=self.start, end_date__gte=self.start),
            narration=f"{MARK} Depreciation brought forward on the asset register",
            reference=f"{MARK}-ASSETS-BF",
        )
        JournalLine.objects.create(entry=entry, line_no=1, account=self.account("3200"),
                                   debit=brought_forward, credit=0, description="Brought forward")
        JournalLine.objects.create(entry=entry, line_no=2, account=self.account("1900"),
                                   debit=0, credit=brought_forward, description="Accumulated depreciation")
        post_journal(entry, actor_user=self.actor)
        for asset in assets:
            if asset.asset_status == "ACTIVE":
                post_depreciation(asset, up_to_date=self.as_of, actor_user=self.actor)
        self.say(f"{len(assets)} assets brought forward, this month's depreciation posted")
