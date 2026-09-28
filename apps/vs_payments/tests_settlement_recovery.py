"""Money that moves at the provider reaches the books once, at the right figure and date.

Greenfield pays Adeola Printers N10,000 for exam booklets, with N500 withheld
for the tax office. Paystack must send Adeola N9,500, and the books must say
N9,500 left the bank, or the school pays the N500 twice. A parent who closes a
checkout (Paystack calls it abandoned) and then finishes paying must still have
the receipt booked, once. A payment whose webhook never arrives must still be
found and booked, on the day the parent paid.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_config.clock import tenant_today, tenant_zone
from vs_finance.models import Account, FinanceAuditLog, FiscalPeriod, FiscalYear, Payment, TaxCode
from vs_procurement.models import VendorPayment

from . import alerts, recovery, services, webhooks
from .constants import CollectionStatus, PayoutStatus, WebhookStatus
from .exceptions import DuplicateWebhookError
from .models import CollectionIntent, PaymentEvent, PayoutInstruction, WebhookEvent
from .tests import _PaymentsFixtureMixin


def _approval_passes(batch, instance):
    """Stand-in for the approval check, which PayoutBatchApprovalTests covers."""
    return instance


class PayoutWithholdingTests(_PaymentsFixtureMixin, TestCase):
    """A payout line's amount is the gross; the supplier receives it less WHT."""

    def _wht_code(self, entity, rate_bps=500):
        return TaxCode.objects.create(
            entity=entity, code="WHT-5", name="WHT 5%", rate_bps=rate_bps,
            collected_account=Account.objects.get(entity=entity, code="2300"),
        )

    def _dispatch(self, batch):
        """Send an approved batch to the Fake provider and return its only instruction."""
        with patch.object(services, "_validate_approved_instance", side_effect=_approval_passes):
            services.submit_payout_batch(batch, approved_instance=object())
        return batch.instructions.get()

    def test_a_wht_payout_transfers_the_net_and_books_the_bank_credit_that_left(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(
            entity=entity,
            items=[{"amount": 1_000_000, "wht_amount": 50_000, "vendor": vendor}],
        )

        payout = self._dispatch(batch)

        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(payout.raw_response["amount"], 950_000)  # What the provider was asked to send.
        self.assertEqual(payout.metadata["transfer_amount"], 950_000)

        self.fake.forced_status[payout.reference] = "PAID"
        self.fake.forced_amount[payout.reference] = 950_000  # The provider reports what it sent.
        payout = services.confirm_payout(payout)

        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(payout.amount, 1_000_000)  # The gross is never overwritten with the net.
        self.assertNotIn("instructed_amount", payout.metadata)
        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(
            (vp.gross_amount, vp.wht_amount, vp.net_amount), (1_000_000, 50_000, 950_000))
        bank_credit = vp.journal.lines.get(account=vp.payment_account).credit
        self.assertEqual(bank_credit, payout.raw_response["amount"])  # The books match the money.
        wht_credit = vp.journal.lines.get(account__code="2300").credit
        self.assertEqual(wht_credit, 50_000)

    def test_a_short_transfer_books_what_left_and_keeps_the_wht(self):
        entity, _, vendor = self.build()
        batch = services.create_payout_batch(
            entity=entity,
            items=[{"amount": 1_000_000, "wht_amount": 50_000, "vendor": vendor}],
        )
        payout = self._dispatch(batch)

        self.fake.forced_status[payout.reference] = "PAID"
        self.fake.forced_amount[payout.reference] = 900_000
        payout = services.confirm_payout(payout)

        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.net_amount, 900_000)
        self.assertEqual(vp.wht_amount, 50_000)
        self.assertEqual(vp.gross_amount, 950_000)
        self.assertEqual(payout.metadata["instructed_amount"], 1_000_000)

    def test_wht_defaults_from_the_vendors_rate_rounded_half_up(self):
        entity, _, vendor = self.build()
        code = self._wht_code(entity)
        vendor.default_wht_tax_code = code
        vendor.save(update_fields=["default_wht_tax_code", "updated_at"])

        batch = services.create_payout_batch(
            entity=entity,
            items=[
                {"amount": 1_000_000, "vendor": vendor},
                {"amount": 333_333, "vendor": vendor},  # 5% is 16,666.65 kobo.
            ],
        )

        first, second = batch.instructions.order_by("pk")
        self.assertEqual(first.metadata["wht_amount"], 50_000)
        self.assertEqual(first.metadata["wht_source"], "COMPUTED")
        self.assertEqual(first.metadata["wht_tax_code"], "WHT-5")
        self.assertEqual(second.metadata["wht_amount"], 16_667)
        created = PaymentEvent.objects.get(action="PAYOUT_BATCH_CREATED", reference=batch.reference)
        self.assertEqual(
            [line["wht_source"] for line in created.metadata["wht"]], ["COMPUTED", "COMPUTED"])

    def test_a_typed_wht_is_kept_and_flagged_through_to_the_booking(self):
        entity, _, vendor = self.build()
        vendor.default_wht_tax_code = self._wht_code(entity)
        vendor.save(update_fields=["default_wht_tax_code", "updated_at"])
        batch = services.create_payout_batch(
            entity=entity,
            items=[{"amount": 1_000_000, "wht_amount": 20_000, "vendor": vendor}],  # A reduced rate.
        )
        payout = self._dispatch(batch)
        self.assertEqual(payout.metadata["wht_amount"], 20_000)
        self.assertEqual(payout.metadata["wht_source"], "ENTERED")

        self.fake.forced_status[payout.reference] = "PAID"
        payout = services.confirm_payout(payout)

        vp = VendorPayment.objects.get(pk=payout.vendor_payment_id)
        self.assertEqual(vp.wht_amount, 20_000)
        self.assertEqual(vp.wht_source, "ENTERED")
        self.assertEqual(vp.wht_tax_code.code, "WHT-5")
        posted = FinanceAuditLog.objects.get(
            action="VENDOR_PAYMENT_POSTED", target_id=str(vp.pk))
        self.assertEqual(posted.metadata["wht_source"], "ENTERED")
        confirmed = PaymentEvent.objects.get(action="PAYOUT_CONFIRMED", reference=payout.reference)
        self.assertEqual(confirmed.metadata["wht_source"], "ENTERED")

    def test_an_explicit_zero_is_kept_even_when_the_vendor_has_a_rate(self):
        entity, _, vendor = self.build()
        vendor.default_wht_tax_code = self._wht_code(entity)
        vendor.save(update_fields=["default_wht_tax_code", "updated_at"])
        batch = services.create_payout_batch(
            entity=entity, items=[{"amount": 1_000_000, "wht_amount": 0, "vendor": vendor}],
        )
        line = batch.instructions.get()
        self.assertEqual(line.metadata["wht_amount"], 0)
        self.assertEqual(line.metadata["wht_source"], "ENTERED")

    def test_wht_must_leave_something_to_send(self):
        entity, _, vendor = self.build()
        with self.assertRaises(ValidationError):
            services.create_payout_batch(
                entity=entity,
                items=[{"amount": 50_000, "wht_amount": 50_000, "vendor": vendor}],
            )


class LateSuccessTests(_PaymentsFixtureMixin, TestCase):
    """A success the provider confirms after an abandoned or failed answer still books."""

    def _charge_success(self, intent):
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=intent.reference, status="SUCCEEDED",
            amount=int(intent.amount),
        )
        with self.captureOnCommitCallbacks(execute=True):
            event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        event.refresh_from_db()
        return event, raw, headers

    def test_a_success_after_abandoned_books_once_and_a_repeat_does_not_double_book(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=320_000, customer=customer)
        self.fake.forced_status[intent.reference] = "ABANDONED"  # The bursar verifies too early.
        intent = services.confirm_collection(intent)
        self.assertEqual(intent.status, CollectionStatus.ABANDONED)
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # The parent finishes paying.
        event, raw, headers = self._charge_success(intent)

        self.assertEqual(event.status, WebhookStatus.PROCESSED)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)
        confirmed = PaymentEvent.objects.get(action="COLLECTION_CONFIRMED", reference=intent.reference)
        self.assertEqual(confirmed.metadata["overturned_status"], "ABANDONED")

        with self.assertRaises(DuplicateWebhookError):  # The provider re-delivers.
            webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        services.confirm_collection(intent)  # Somebody presses verify again.
        webhooks.process_stored_event(event.id)  # And an operator replays it.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_success_after_failed_books(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=45_000, customer=customer)
        services.confirm_collection(intent, status=CollectionStatus.FAILED)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        intent = services.confirm_collection(intent)

        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_repeated_no_money_answer_is_audited_once(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=45_000, customer=customer)
        self.fake.forced_status[intent.reference] = "ABANDONED"
        services.confirm_collection(intent)
        services.confirm_collection(intent)
        self.assertEqual(
            PaymentEvent.objects.filter(
                action="COLLECTION_FAILED", reference=intent.reference).count(),
            1,
        )

    def test_a_success_webhook_that_books_nothing_is_not_marked_processed(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=30_000, customer=customer)
        # No forced status: the provider's API still says pending.
        event, _raw, _headers = self._charge_success(intent)

        self.assertEqual(event.status, WebhookStatus.FAILED)
        self.assertIn("nothing was booked", event.error)
        self.assertIn(event.status, alerts.UNBOOKED_STATUSES)  # The unbooked digest counts it.
        self.assertFalse(Payment.objects.filter(entity=entity).exists())

        self.fake.forced_status[intent.reference] = "SUCCEEDED"  # The provider catches up.
        webhooks.process_stored_event(event.id)  # A replay now books it.
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.PROCESSED)
        self.assertEqual(event.error, "")
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)


class RecoverySweepTests(_PaymentsFixtureMixin, TestCase):
    """The sweep finds settled money no webhook delivered, and books it on the paid day."""

    def _age(self, model, pk, *, minutes):
        """Make a row look ``minutes`` old and last touched then."""
        then = timezone.now() - datetime.timedelta(minutes=minutes)
        model.objects.filter(pk=pk).update(created_at=then, updated_at=then)

    def _cover(self, entity, day):
        """Make sure a fiscal period covers ``day``, adding that month if none does."""
        if FiscalPeriod.objects.filter(
                entity=entity, start_date__lte=day, end_date__gte=day).exists():
            return
        year, _ = FiscalYear.objects.get_or_create(
            entity=entity, year=day.year,
            defaults={"start_date": datetime.date(day.year, 1, 1),
                      "end_date": datetime.date(day.year, 12, 31)},
        )
        start = day.replace(day=1)
        end = (start + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=day.month,
            name=f"{day.year}-{day.month:02d}", start_date=start, end_date=end,
        )

    def _paid_at(self, entity, day):
        """Noon on ``day`` at the school, as an aware instant."""
        return datetime.datetime.combine(
            day, datetime.time(12, 0), tzinfo=tenant_zone(entity.tenant))

    def test_the_sweep_books_a_pending_success_dated_the_day_the_parent_paid(self):
        entity, customer, _ = self.build()
        today = tenant_today(entity.tenant)
        paid_on = today - datetime.timedelta(days=3)
        self._cover(entity, paid_on)
        intent = services.initiate_collection(entity=entity, amount=400_000, customer=customer)
        self._age(CollectionIntent, intent.pk, minutes=24 * 60 * 3)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self.fake.forced_paid_at[intent.reference] = self._paid_at(entity, paid_on)

        summary = recovery.recover_unconfirmed_payments()

        self.assertEqual(summary["collections_booked"], 1)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        payment = Payment.objects.get(pk=intent.payment_id)
        self.assertEqual(payment.payment_date, paid_on)
        self.assertEqual(intent.metadata["paid_on"], paid_on.isoformat())
        self.assertNotIn("booked_on", intent.metadata)

        recovery.recover_unconfirmed_payments()  # A second run books nothing more.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_paid_day_in_a_closed_period_books_on_the_first_open_day_after_it(self):
        entity, customer, _ = self.build()
        today = tenant_today(entity.tenant)
        paid_on = today - datetime.timedelta(days=40)
        self._cover(entity, paid_on)
        FiscalPeriod.objects.filter(
            entity=entity, start_date__lte=paid_on, end_date__gte=paid_on,
        ).update(status="CLOSED")
        first_open = (
            FiscalPeriod.objects.filter(entity=entity, status="OPEN", start_date__gt=paid_on)
            .order_by("start_date").first().start_date
        )
        intent = services.initiate_collection(entity=entity, amount=400_000, customer=customer)
        self._age(CollectionIntent, intent.pk, minutes=60)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self.fake.forced_paid_at[intent.reference] = self._paid_at(entity, paid_on)

        recovery.recover_unconfirmed_payments()

        intent.refresh_from_db()
        payment = Payment.objects.get(pk=intent.payment_id)
        self.assertEqual(payment.payment_date, first_open)
        self.assertEqual(intent.metadata["paid_on"], paid_on.isoformat())  # The true day is kept.
        self.assertEqual(intent.metadata["booked_on"], first_open.isoformat())

    def test_a_young_collection_is_left_to_its_webhook(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=10_000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        summary = recovery.recover_unconfirmed_payments()

        self.assertEqual(summary["collections"], 0)
        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)

    def test_an_abandoned_checkout_paid_later_is_booked_by_the_sweep(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=10_000, customer=customer)
        self.fake.forced_status[intent.reference] = "ABANDONED"
        services.confirm_collection(intent)
        self._age(CollectionIntent, intent.pk, minutes=90)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        recovery.recover_unconfirmed_payments()

        intent.refresh_from_db()
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)

    def test_a_lost_webhook_task_is_re_run(self):
        entity, customer, _ = self.build()
        intent = services.initiate_collection(entity=entity, amount=25_000, customer=customer)
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        raw, headers = self.fake.build_webhook(
            event="charge.success", reference=intent.reference, status="SUCCEEDED",
            amount=25_000,
        )
        # No on-commit capture: the processing task is lost, the event stays RECEIVED.
        event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=raw, headers=headers)
        self._age(WebhookEvent, event.pk, minutes=20)

        summary = recovery.recover_unconfirmed_payments()

        self.assertEqual(summary["events"], 1)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookStatus.PROCESSED)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_processing_payout_is_booked_and_its_failed_event_closed(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(entity, vendor, amount=70_000)
        stale = WebhookEvent.objects.create(
            provider="PAYSTACK", event_type="transfer.success", dedupe_key="stale-transfer",
            status=WebhookStatus.FAILED, error="Worker restarted.", payout=payout,
        )
        self._age(PayoutInstruction, payout.pk, minutes=45)
        self.fake.forced_status[payout.reference] = "PAID"

        summary = recovery.recover_unconfirmed_payments()

        self.assertEqual(summary["payouts_booked"], 1)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertEqual(VendorPayment.objects.filter(entity=entity).count(), 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, WebhookStatus.PROCESSED)  # No longer reported as unbooked.

    def test_a_payout_still_in_flight_is_asked_again_later_not_every_run(self):
        entity, _, vendor = self.build()
        payout = self.make_processing_payout(entity, vendor, amount=70_000)
        self._age(PayoutInstruction, payout.pk, minutes=45)

        first = recovery.recover_unconfirmed_payments()
        second = recovery.recover_unconfirmed_payments()

        self.assertEqual(first["payouts"], 1)
        self.assertEqual(second["payouts"], 0)  # Asked a moment ago; backs off.
        self.assertFalse(PaymentEvent.objects.filter(action="PAYOUT_FAILED").exists())
