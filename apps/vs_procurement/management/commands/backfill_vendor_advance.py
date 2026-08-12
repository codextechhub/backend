"""Reclassify existing vendor prepayments out of AP into the 1240 advance asset.

Before vendor prepayments were modelled as an asset, a vendor payment debited AP for
its whole gross whether or not it settled anything. Pay a supplier on 1 March and take
their bill on the 10th, and AP - a liability - carried a *debit* balance for those nine
days, which reads as "our suppliers owe us money". This one-time backfill posts
``Dr 1240 · Cr AP`` per affected payment so the books match the new model: AP holds only
what is owed, and money paid ahead of a bill is an asset.

It also re-seeds the chart, which is the part that matters even where nothing needs
reclassifying: an entity created before 1240 existed has no advance account, and the
first prepayment posted there would fail rather than post to the wrong place.

Dry-run by default; pass ``--commit`` to post. Idempotent: a per-payment journal
reference guards against double-posting on re-run.

    python manage.py backfill_vendor_advance                        # show, post nothing
    python manage.py backfill_vendor_advance --commit                # all entities
    python manage.py backfill_vendor_advance --entity CODEX --commit
"""
from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from vs_finance.constants import DocumentStatus, JournalSource
from vs_finance.posting import post_journal, resolve_period
from vs_finance.seed import seed_chart_of_accounts

from vs_procurement.constants import VENDOR_ADVANCE_CODE
from vs_procurement.purchasing import resolve_account


class Command(BaseCommand):
    help = "Reclassify vendor prepayments sitting in AP into the 1240 advance asset."

    def add_arguments(self, parser):
        parser.add_argument("--entity", help="Entity code (default: all entities).")
        parser.add_argument("--commit", action="store_true",
                            help="Actually post the reclass journals (default: dry-run).")

    def handle(self, *args, **opts):
        from vs_finance.models import JournalEntry, JournalLine, LedgerEntity
        from vs_procurement.models import VendorPayment

        entities = LedgerEntity.objects.all()
        if opts.get("entity"):
            entities = entities.filter(code=opts["entity"])
        commit = opts.get("commit")
        today = datetime.date.today()
        total_posted = 0

        for entity in entities:
            seed_chart_of_accounts(entity)  # ensures 1240 exists on older entities
            payments = (
                VendorPayment.objects
                .filter(entity=entity, status=DocumentStatus.POSTED, journal__isnull=False)
                .select_related("vendor", "journal").order_by("payment_date", "pk")
            )
            for payment in payments:
                ap_account = payment.vendor.payable_account
                if ap_account is None:
                    continue  # Nothing was debited to AP; nothing to move out of it.
                # Read what actually hit AP rather than trusting the gross: that debit
                # is the money in the wrong place, and it is what has to come back out.
                ap_debit = sum(
                    int(value or 0) for value in payment.journal.lines
                    .filter(account_id=ap_account.pk).values_list("debit", flat=True)
                )
                advance = ap_debit - int(payment.allocated_amount)
                if advance <= 0:
                    continue  # Everything this payment put into AP settled a bill.
                ref = f"VA-BACKFILL-{payment.document_number or payment.pk}"
                if JournalEntry.objects.filter(entity=entity, reference=ref).exists():
                    self.stdout.write(f"  skip {entity.code}/{ref}: already backfilled")
                    continue
                label = payment.document_number or payment.pk
                self.stdout.write(
                    f"  {entity.code}/{label} ({payment.vendor.code}, paid "
                    f"{payment.payment_date}): reclass {advance} kobo "
                    f"Dr 1240 / Cr {ap_account.code}",
                )
                if not commit:
                    continue
                with transaction.atomic():
                    entry = JournalEntry.objects.create(
                        entity=entity, branch=payment.branch,
                        date=today, period=resolve_period(entity, today),
                        source=JournalSource.PURCHASE, currency=payment.currency,
                        narration=f"Vendor advance backfill: {payment.vendor.code}",
                        reference=ref,
                    )
                    JournalLine.objects.create(
                        entry=entry,
                        account=resolve_account(entity, VENDOR_ADVANCE_CODE, label="vendor advances"),
                        debit=advance, credit=0,
                        description=f"Vendor advance: {payment.vendor.code}", line_no=1,
                    )
                    JournalLine.objects.create(
                        entry=entry, account=ap_account, debit=0, credit=advance,
                        description=f"AP: {payment.vendor.code}", line_no=2,
                    )
                    post_journal(entry)
                total_posted += 1

        verb = "Posted" if commit else "Would post"
        self.stdout.write(self.style.SUCCESS(f"{verb} {total_posted} reclass journal(s)."))
