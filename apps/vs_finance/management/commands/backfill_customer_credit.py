"""Reclassify existing customer credit out of AR into the 2140 liability.

Before customer credit was modelled as a liability, an overpayment / unapplied
credit left the customer's AR control with a *credit* balance. This one-time
backfill posts ``Dr AR · Cr 2140`` per customer for their available credit, so
the books match the new model (AR holds only what's owed; credit is a liability).

Dry-run by default; pass ``--commit`` to post. Idempotent: a per-customer journal
reference guards against double-posting on re-run.

    python manage.py backfill_customer_credit --commit            # all entities
    python manage.py backfill_customer_credit --entity CODEX --commit
"""
from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_finance.constants import CUSTOMER_CREDIT_CODE, JournalSource
from vs_finance.accounts import resolve_account
from vs_finance.posting import post_journal, resolve_period
from vs_finance.receivables import customer_credit_balance
from vs_finance.seed import seed_chart_of_accounts


class Command(BaseCommand):
    help = "Reclassify existing negative-AR customer credit into the 2140 liability."

    def add_arguments(self, parser):
        parser.add_argument("--entity", help="Entity code (default: all entities).")
        parser.add_argument("--commit", action="store_true",
                            help="Actually post the reclass journals (default: dry-run).")

    def handle(self, *args, **opts):
        from vs_finance.models import Customer, JournalEntry, JournalLine, LedgerEntity

        entities = LedgerEntity.objects.all()
        if opts.get("entity"):
            entities = entities.filter(code=opts["entity"])
        commit = opts.get("commit")
        today = datetime.date.today()
        total_posted = 0

        for entity in entities:
            seed_chart_of_accounts(entity)  # ensures 2140 exists on older entities
            for customer in Customer.objects.filter(entity=entity):
                credit = customer_credit_balance(customer)
                if credit <= 0:
                    continue
                ref = f"CC-BACKFILL-{customer.code}"
                if JournalEntry.objects.filter(entity=entity, reference=ref).exists():
                    self.stdout.write(f"  skip {entity.code}/{customer.code}: already backfilled")
                    continue
                if customer.receivable_account_id is None:
                    self.stdout.write(self.style.WARNING(
                        f"  skip {entity.code}/{customer.code}: no AR control account"))
                    continue
                self.stdout.write(f"  {entity.code}/{customer.code}: reclass {credit} kobo → 2140")
                if not commit:
                    continue
                with transaction.atomic():
                    entry = JournalEntry.objects.create(
                        entity=entity, date=today, period=resolve_period(entity, today),
                        source=JournalSource.SALES,
                        narration=f"Customer credit backfill: {customer.code}", reference=ref,
                    )
                    JournalLine.objects.create(
                        entry=entry, account=customer.receivable_account, debit=credit, credit=0,
                        description=f"AR: {customer.code}", line_no=1,
                    )
                    JournalLine.objects.create(
                        entry=entry, account=resolve_account(entity, CUSTOMER_CREDIT_CODE, label="customer credit"),
                        debit=0, credit=credit, description=f"Customer credit: {customer.code}", line_no=2,
                    )
                    post_journal(entry)
                total_posted += 1

        verb = "Posted" if commit else "Would post"
        self.stdout.write(self.style.SUCCESS(f"{verb} {total_posted} reclass journal(s)."))
