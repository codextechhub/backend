"""Seed a school's books with the receivables activity the Receivables tab reads.

A development aid, run after ``seed_finance_dashboard_demo`` on the same books.
It adds what that command does not: reminders at two escalation levels (some
followed by payment within a week), concessions of each kind, a credit note,
overpayments that leave payers in credit, a refund awaiting approval and two
write-off requests awaiting approval.

Everything is posted through the normal finance services where one exists and
is marked ``DEMO``. Reminders are raised by the real dunning run for two dates
and marked sent without dispatching anything, and customer email copies are
switched off, so no demo document reaches a parent.

Refuses to run twice on the same books, and on production settings.

Usage::

    manage.py seed_finance_receivables_demo --entity HOLYCROSS
"""
from __future__ import annotations

import datetime
import random
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

MARK = "DEMO"


class Command(BaseCommand):
    help = "Seed demo receivables activity (reminders, concessions, credit) on one school's books."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="LedgerEntity code, e.g. HOLYCROSS.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data is for development databases only.")
        from vs_finance.models import Concession, LedgerEntity

        entity = LedgerEntity.objects.filter(code=options["entity"]).first()
        if entity is None:
            raise CommandError(f"No entity with code {options['entity']}.")
        if Concession.objects.filter(entity=entity, reference__startswith=MARK).exists():
            raise CommandError(f"{entity.code} already holds the receivables demo data.")
        # The copies are sent from on-commit hooks, so the switches must stay on
        # until the transaction has committed: they wrap it, not sit inside it.
        with mock.patch("vs_finance.document_email.issue_invoice_copy", return_value=None), \
                mock.patch("vs_finance.document_email.issue_receipt_copy", return_value=None), \
                transaction.atomic():
            Seeder(entity, datetime.date.today(), random.Random(f"{entity.code}-ar"), self.stdout).run()


class Seeder:
    def __init__(self, entity, as_of, rng, out):
        from django.contrib.auth import get_user_model

        self.entity, self.as_of, self.rng, self.out = entity, as_of, rng, out
        self.actor = get_user_model().objects.filter(tenant=entity.tenant, is_active=True).order_by("id").first()

    def say(self, text):
        self.out.write(f"  {text}")

    def account(self, code):
        from vs_finance.models import Account

        return Account.objects.get(entity=self.entity, code=code)

    def open_invoices(self, **filters):
        from vs_finance.models import Invoice

        return list(
            Invoice.objects.filter(entity=self.entity, status="POSTED", **filters)
            .annotate(bal=F("total") - F("amount_paid") - F("amount_credited"))
            .filter(bal__gt=0).select_related("customer").order_by("id")
        )

    def run(self):
        self.out.write(f"Seeding receivables demo data on {self.entity.code}:")
        self.reminders()
        self.concessions()
        self.credit_note()
        self.overpayments()
        self.pending_adjustments()
        self.out.write("Done.")

    def reminders(self):
        from vs_finance.dunning import ensure_default_policy, generate_dunning
        from vs_finance.models import DunningNotice, Payment
        from vs_finance.receivables import post_payment

        policy = ensure_default_policy(self.entity)
        first_run = self.as_of - datetime.timedelta(days=10)
        early = generate_dunning(self.entity, as_of=first_run, policy=policy, actor_user=self.actor)
        # A few parents pay within the week after their first reminder.
        for n, notice in enumerate(early[:3]):
            invoice = notice.invoice
            invoice.refresh_from_db()
            balance = invoice.total - invoice.amount_paid - invoice.amount_credited
            if balance <= 0:
                continue
            payment = Payment.objects.create(
                entity=self.entity, customer=invoice.customer, branch=invoice.branch,
                payment_date=first_run + datetime.timedelta(days=2 + n), amount=balance, method="BANK_TRANSFER",
                deposit_account=self.account("1100"), reference=f"{MARK}-REMINDED-{n + 1}",
            )
            post_payment(payment, actor_user=self.actor, allocations=[(invoice, balance)])
        later = generate_dunning(self.entity, as_of=self.as_of, policy=policy, actor_user=self.actor)
        DunningNotice.objects.filter(pk__in=[n.pk for n in [*early, *later]]).exclude(
            notice_status="RESOLVED",
        ).update(notice_status="SENT")
        self.say(f"{len(early) + len(later)} reminders over two runs, 3 paid within a week")

    def concessions(self):
        from vs_finance.installments import post_concession
        from vs_finance.models import Concession

        invoices = [i for i in self.open_invoices(reference__startswith="FEE:") if i.bal >= 50_000_00]
        self.rng.shuffle(invoices)
        specs = [("DISCOUNT", "Sibling discount", 0.10)] * 4 + [("WAIVER", "Staff child", 0.25)] * 2 \
            + [("SCHOLARSHIP", "Academic scholarship", 0.5)]
        made = 0
        for (kind, reason, share), invoice in zip(specs, invoices):
            amount = int(invoice.bal * share) // 100 * 100
            concession = Concession.objects.create(
                entity=self.entity, customer=invoice.customer, invoice=invoice, branch=invoice.branch,
                kind=kind, concession_date=max(invoice.invoice_date, self.as_of - datetime.timedelta(days=5)),
                amount=amount, reason=reason, reference=f"{MARK}-CONC-{made + 1}",
            )
            post_concession(concession, actor_user=self.actor)
            made += 1
        self.say(f"{made} concessions across three kinds")

    def credit_note(self):
        from vs_finance.credit_notes import post_credit_note
        from vs_finance.models import CreditNote, CreditNoteLine

        invoice = next(iter(self.open_invoices(reference__startswith="FEE:")), None)
        if invoice is None:
            return
        note = CreditNote.objects.create(
            entity=self.entity, customer=invoice.customer, branch=invoice.branch, kind="CREDIT",
            note_date=self.as_of - datetime.timedelta(days=3), invoice=invoice,
            reason="Billed for boarding; the child is a day student", reference=f"{MARK}-CN-1",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, description="Boarding fee reversed", revenue_account=self.account("4100"),
            quantity=1, unit_price=min(invoice.bal, 45_000_00),
        )
        post_credit_note(note, actor_user=self.actor, allocations=[(invoice, min(invoice.bal, 45_000_00))])
        self.say("1 credit note applied")

    def overpayments(self):
        from vs_finance.models import Payment
        from vs_finance.receivables import post_payment

        invoices = self.open_invoices(reference__startswith="FEE:")[-3:]
        for n, invoice in enumerate(invoices):
            extra = self.rng.choice([15_000_00, 25_000_00, 40_000_00])
            payment = Payment.objects.create(
                entity=self.entity, customer=invoice.customer, branch=invoice.branch,
                payment_date=self.as_of - datetime.timedelta(days=1), amount=invoice.bal + extra,
                method="ONLINE", deposit_account=self.account("1100"), reference=f"{MARK}-OVERPAID-{n + 1}",
            )
            post_payment(payment, actor_user=self.actor, allocations=[(invoice, invoice.bal)])
        self.say(f"{len(invoices)} overpayments left as credit")

    def pending_adjustments(self):
        from vs_finance.models import Refund, WriteOffRequest

        from vs_finance.models import Payment

        credit = Payment.objects.filter(entity=self.entity, reference__startswith=f"{MARK}-OVERPAID").first()
        if credit is not None:
            Refund.objects.create(
                entity=self.entity, customer=credit.customer, branch=credit.branch,
                refund_date=self.as_of, amount=credit.amount - credit.allocated_amount,
                method="BANK_TRANSFER", reference=f"{MARK}-REF-1", narration="Overpayment returned to the parent",
                status="PENDING_APPROVAL",
            )
        arrears = self.open_invoices(reference__startswith=f"{MARK}-ARREARS")[:2]
        for n, invoice in enumerate(arrears):
            WriteOffRequest.objects.create(
                entity=self.entity, invoice=invoice, branch=invoice.branch, amount=invoice.bal,
                reason="Family relocated; balance not recoverable", status="PENDING_APPROVAL",
            )
        self.say(f"1 refund and {len(arrears)} write-offs awaiting approval")
