"""Seed a school's books with enough activity to read the finance dashboard filled in.

A development aid. Every card on the finance overview needs activity behind it to
be checked with real numbers rather than in its empty state: receipts across
payment methods, overdue payers, bank accounts with statement lines waiting to be
matched, payment plans (some behind), petty cash floats (one running low), an
accrued payroll run, tax filings coming due, and a budget with spending lines.

Everything is posted through the normal finance services, dated inside the
entity's open periods, and marked ``DEMO`` in its reference or name so it can be
told apart from anything a person entered. Receipts settle the entity's existing
fee invoices, so run it on books that already hold a term's billing.

Refuses to run twice on the same books (it looks for its own marker), and
refuses on production settings. Posting normally emails the customer a copy of
each invoice and receipt; the seed switches those copies off, so demo documents
never reach a parent's inbox.

Usage::

    manage.py seed_finance_dashboard_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
import random
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

MARK = "DEMO"


class Command(BaseCommand):
    help = "Seed demo finance activity for the dashboard on one school's books."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")
        parser.add_argument("--as-of", help="The day the demo is seeded up to (default today).")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import BankAccount, LedgerEntity

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if BankAccount.objects.filter(entity=entity, name__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the demo data.")
        as_of = (datetime.date.fromisoformat(options["as_of"]) if options.get("as_of")
                 else datetime.date.today())
        rng = random.Random(entity.code)
        with transaction.atomic(), \
                mock.patch("vs_finance.document_email.issue_invoice_copy", return_value=None), \
                mock.patch("vs_finance.document_email.issue_receipt_copy", return_value=None):
            Seeder(entity, as_of, rng, self.stdout).run()


class Seeder:
    """One run of the demo seed over one entity."""

    def __init__(self, entity, as_of, rng, out):
        from django.contrib.auth import get_user_model

        from vs_finance.models import FiscalPeriod

        self.entity, self.as_of, self.rng, self.out = entity, as_of, rng, out
        period = FiscalPeriod.objects.filter(
            entity=entity, status="OPEN", start_date__lte=as_of, end_date__gte=as_of,
        ).first()
        if period is None:
            raise CommandError(f"{entity.code} has no open period containing {as_of}.")
        self.start = max(period.start_date, as_of - datetime.timedelta(days=25))
        self.actor = (
            get_user_model().objects.filter(tenant=entity.tenant, is_active=True).order_by("id").first()
        )

    def say(self, text):
        self.out.write(f"  {text}")

    def day(self, lo=0, hi=None):
        """A date between ``start + lo`` and ``as_of`` (or ``start + hi``)."""
        span = (self.as_of - self.start).days
        hi = span if hi is None else min(hi, span)
        return self.start + datetime.timedelta(days=self.rng.randint(lo, max(lo, hi)))

    def account(self, code):
        from vs_finance.models import Account

        return Account.objects.get(entity=self.entity, code=code)

    def run(self):
        self.out.write(f"Seeding dashboard demo data on {self.entity.code} up to {self.as_of}:")
        banks = self.bank_accounts()
        self.opening_balances(banks)
        self.arrears()
        self.receipts(banks)
        self.statement_lines(banks["collections"])
        self.payment_plans()
        self.petty_cash(banks["operations"])
        self.payroll(banks["payroll"])
        self.tax_filings()
        self.budget()
        self.out.write("Done.")

    # -- bank accounts -------------------------------------------------------- #

    def bank_accounts(self):
        from vs_finance.models import Account, BankAccount

        parent = self.account("1000")
        cash = self.account("1100")

        def ledger(code, name):
            account, _ = Account.objects.get_or_create(
                entity=self.entity, code=code,
                defaults={"name": name, "account_type": "ASSET", "parent": parent, "is_postable": True},
            )
            return account

        specs = {
            "collections": (f"{MARK} GTBank collections", "Guaranty Trust Bank", cash, True),
            "operations": (f"{MARK} Zenith operations", "Zenith Bank", ledger("1101", "Zenith Operations"), False),
            "payroll": (f"{MARK} Access payroll", "Access Bank", ledger("1102", "Access Payroll"), False),
        }
        banks = {}
        for key, (name, bank_name, gl, collection) in specs.items():
            banks[key] = BankAccount.objects.create(
                entity=self.entity, name=name, bank_name=bank_name,
                account_number=f"01{self.rng.randint(10_000_000, 99_999_999)}",
                gl_account=gl, is_active=True, is_primary=key == "operations",
                is_primary_collection=collection,
            )
        self.say("3 bank accounts")
        return banks

    def opening_balances(self, banks):
        from vs_finance.models import FiscalPeriod, JournalEntry, JournalLine
        from vs_finance.posting import post_journal

        date = self.start
        entry = JournalEntry.objects.create(
            entity=self.entity, date=date,
            period=FiscalPeriod.objects.get(entity=self.entity, start_date__lte=date, end_date__gte=date),
            narration=f"{MARK} Balances brought forward from the bank",
            reference=f"{MARK}-OPENING",
        )
        amounts = {"operations": 14_800_000_00, "payroll": 26_000_000_00}
        for key, amount in amounts.items():
            JournalLine.objects.create(entry=entry, account=banks[key].gl_account, debit=amount, credit=0,
                                       description="Balance at the bank")
        JournalLine.objects.create(entry=entry, account=self.account("3200"), debit=0,
                                   credit=sum(amounts.values()), description="Brought forward")
        post_journal(entry, actor_user=self.actor)
        self.say("opening bank balances")

    # -- receivables ---------------------------------------------------------- #

    def open_invoices(self):
        from django.db.models import F

        from vs_finance.models import Invoice

        return list(
            Invoice.objects.filter(entity=self.entity, status="POSTED")
            .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
            .filter(bal__gt=0).select_related("customer").order_by("id")
        )

    def arrears(self):
        """Last term's unpaid balances, carried into this term and already past due."""
        from vs_finance.models import Customer, Invoice, InvoiceLine
        from vs_finance.receivables import post_invoice

        customers = list(Customer.objects.filter(entity=self.entity).order_by("id"))
        self.rng.shuffle(customers)
        revenue = self.account("4100")
        for customer in customers[:9]:
            issued = self.start
            invoice = Invoice.objects.create(
                entity=self.entity, customer=customer, branch=customer.branch,
                invoice_date=issued, due_date=issued + datetime.timedelta(days=self.rng.choice([3, 7, 10])),
                reference=f"{MARK}-ARREARS-2526T3",
            )
            InvoiceLine.objects.create(
                invoice=invoice, line_no=1, quantity=1, revenue_account=revenue,
                description="Balance brought forward, Third Term 2025/2026",
                unit_price=self.rng.choice([45_000_00, 60_000_00, 85_000_00, 120_000_00, 150_000_00]),
            )
            post_invoice(invoice, actor_user=self.actor)
        self.say("9 arrears invoices from last term")

    def receipts(self, banks):
        from vs_finance.models import Payment
        from vs_finance.receivables import post_payment

        methods = (["BANK_TRANSFER"] * 5 + ["ONLINE"] * 4 + ["CASH"] * 2 + ["CARD"] * 1)
        invoices = [i for i in self.open_invoices() if not i.reference.startswith(f"{MARK}-ARREARS")]
        self.rng.shuffle(invoices)
        paid = 0
        for n, invoice in enumerate(invoices[: int(len(invoices) * 0.7)]):
            method = self.rng.choice(methods)
            amount = invoice.bal if n % 6 else invoice.bal // 2
            deposit = banks["operations" if method == "CASH" else "collections"].gl_account
            payment = Payment.objects.create(
                entity=self.entity, customer=invoice.customer, branch=invoice.branch,
                payment_date=max(invoice.invoice_date, self.day(0)), amount=amount,
                method=method, deposit_account=deposit, reference=f"{MARK}-RCPT-{n + 1:03d}",
            )
            post_payment(payment, actor_user=self.actor, allocations=[(invoice, amount)])
            paid += 1
        self.say(f"{paid} receipts against this term's fees")

    def statement_lines(self, bank):
        from vs_finance.banking import import_statement_lines

        rows = [
            {"txn_date": self.day(8), "amount": self.rng.choice([85_000_00, 120_000_00, 160_000_00]),
             "description": "NIP transfer from parent, no reference", "reference": f"{MARK}-NIP-{i}"}
            for i in range(6)
        ] + [
            {"txn_date": self.day(10), "amount": -2_500_00, "description": "SMS alert charges",
             "reference": f"{MARK}-CHG-1"},
            {"txn_date": self.day(12), "amount": -7_500_00, "description": "Account maintenance fee",
             "reference": f"{MARK}-CHG-2"},
        ]
        import_statement_lines(bank, rows, statement_date=self.as_of,
                               period_label=f"{MARK} {self.as_of:%B %Y}", actor_user=self.actor)
        self.say(f"{len(rows)} statement lines waiting to be matched")

    def payment_plans(self):
        from vs_finance.installments import activate_payment_plan, build_installments
        from vs_finance.models import PaymentPlan

        invoices = [i for i in self.open_invoices() if i.bal >= 100_000_00][:6]
        for n, invoice in enumerate(invoices):
            first_due = self.start + datetime.timedelta(days=5) if n < 3 else self.as_of + datetime.timedelta(days=5)
            plan = PaymentPlan.objects.create(
                entity=self.entity, customer=invoice.customer, invoice=invoice, branch=invoice.branch,
                start_date=first_due, frequency="MONTHLY", installment_count=3,
                total_amount=invoice.bal, notes=f"{MARK} plan agreed with the bursar",
            )
            build_installments(plan)
            activate_payment_plan(plan, actor_user=self.actor)
        self.say(f"{len(invoices)} payment plans, 3 of them behind")

    # -- operations ----------------------------------------------------------- #

    def petty_cash(self, bank):
        from vs_finance.models import PettyCashFund, PettyCashVoucher, PettyCashVoucherLine
        from vs_finance.petty_cash import establish_fund, post_voucher, price_voucher

        from vs_tenants.models import Branch

        branches = list(Branch.objects.filter(tenant=self.entity.tenant).order_by("id"))
        specs = [("Front desk float", 150_000_00, 40_000_00), ("Kitchen float", 100_000_00, 84_000_00)]
        for n, (name, float_amount, spent) in enumerate(specs):
            gl, _ = self.account_for_petty(n)
            fund = PettyCashFund.objects.create(
                entity=self.entity, branch=branches[n % len(branches)] if branches else None,
                gl_account=gl, name=f"{MARK} {name}", custodian_name="Front office",
                float_amount=float_amount, current_balance=0,
            )
            establish_fund(fund, bank_account=bank, amount=float_amount, date=self.start, actor_user=self.actor)
            voucher = PettyCashVoucher.objects.create(
                entity=self.entity, branch=fund.branch, fund=fund, voucher_date=self.day(5),
                payee="Local suppliers", narration=f"{MARK} small purchases", reference=f"{MARK}-PCV-{n + 1}",
            )
            PettyCashVoucherLine.objects.create(
                voucher=voucher, line_no=1, description="Cleaning materials and fuel for the generator",
                expense_account=self.account("5300"), quantity=1, unit_price=spent,
            )
            price_voucher(voucher)
            post_voucher(voucher, actor_user=self.actor)
        self.say("2 petty cash floats, one running low")

    def account_for_petty(self, n):
        from vs_finance.models import Account

        return Account.objects.get_or_create(
            entity=self.entity, code=f"111{n + 1}",
            defaults={"name": f"Petty Cash Float {n + 1}", "account_type": "ASSET",
                      "parent": self.account("1000"), "is_postable": True},
        )

    def payroll(self, bank):
        from vs_finance.models import PayrollLine, PayrollRun
        from vs_finance.payroll import compute_payroll, post_payroll

        pay_date = min(self.as_of.replace(day=28) if self.as_of.day <= 28 else self.as_of,
                       self.as_of + datetime.timedelta(days=4))
        pay_date = max(pay_date, self.as_of + datetime.timedelta(days=1))
        run = PayrollRun.objects.create(
            entity=self.entity, pay_date=pay_date, period_label=f"{pay_date:%B}",
            narration=f"{MARK} {pay_date:%B} salaries", bank_account=bank,
        )
        for i in range(1, 25):
            gross = self.rng.choice([180_000_00, 220_000_00, 260_000_00, 320_000_00, 450_000_00])
            PayrollLine.objects.create(
                run=run, line_no=i, employee_name=f"Staff member {i}", gross_amount=gross,
                paye_amount=gross * 11 // 100, pension_amount=gross * 8 // 100,
            )
        compute_payroll(run)
        run.refresh_from_db()
        post_payroll(run, actor_user=self.actor)
        self.say(f"{run.period_label} payroll accrued, to be paid {pay_date}")

    def tax_filings(self):
        from vs_finance.models import TaxObligation
        from vs_finance.tax_filing import prepare_filing

        month_start = self.as_of.replace(day=1)
        month_end = (month_start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        due_next = month_end + datetime.timedelta(days=10)
        made = 0
        for code, due in (("PAYE", due_next), ("PENSION", due_next),
                          ("VAT", month_end + datetime.timedelta(days=21)),
                          ("WHT", month_end + datetime.timedelta(days=21))):
            obligation = TaxObligation.objects.filter(entity=self.entity, code=code).first()
            if obligation is None:
                continue
            prepare_filing(obligation, period_start=month_start, period_end=month_end,
                           due_date=due, actor_user=self.actor)
            made += 1
        self.say(f"{made} tax filings prepared")

    def budget(self):
        from vs_finance.budgets import set_budget_lines
        from vs_finance.models import Budget, FiscalPeriod

        period = FiscalPeriod.objects.filter(entity=self.entity, start_date__lte=self.as_of,
                                             end_date__gte=self.as_of).select_related("fiscal_year").first()
        budget = Budget.objects.filter(entity=self.entity, branch__isnull=True,
                                       fiscal_year=period.fiscal_year, status="DRAFT").first()
        if budget is None:
            self.say("no draft school budget to fill; skipped")
            return
        lines = [("4100", 20_000_000_00), ("5200", 9_000_000_00), ("5300", 3_200_000_00),
                 ("5500", 400_000_00)]
        set_budget_lines(budget, [
            {"account": self.account(code), "cost_center": None, "period_no": 1, "amount": amount}
            for code, amount in lines
        ])
        self.say(f"'{budget.name}' given income and spending lines")
