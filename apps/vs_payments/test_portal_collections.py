"""How a collection was paid, what its receipt says it left owing, and who is told.

Acme owes a ₦50,000.00 invoice. It pays ₦20,000.00 by card and then
₦10,000.00 by USSD. Each receipt keeps the balance the invoice had right after
that payment, however much is paid later, and each collection records how it
was actually paid. A collection the payer started in their own account tells
that account when it is confirmed; one staff started does not.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, TestCase

from vs_finance.documents import receipt_document_context
from vs_finance.models import Payment, PaymentAllocation
from vs_user.models import User

from . import services
from .constants import CollectionOrigin, PaidVia
from .providers.paystack import _paid_via
from .tests import _PaymentsFixtureMixin

SEND = "vs_notifications.notify.send_notification"


class PaidViaMappingTests(SimpleTestCase):
    def test_paystack_channels_read_in_the_platforms_words(self):
        self.assertEqual(_paid_via("card"), PaidVia.CARD)
        self.assertEqual(_paid_via("bank_transfer"), PaidVia.BANK_TRANSFER)
        self.assertEqual(_paid_via("bank"), PaidVia.BANK_TRANSFER)
        self.assertEqual(_paid_via("USSD"), PaidVia.USSD)
        self.assertEqual(_paid_via("mobile_money"), PaidVia.OTHER)
        self.assertEqual(_paid_via(None), "")


class PortalCollectionTests(_PaymentsFixtureMixin, TestCase):
    def pay(self, entity, customer, invoice, amount, *, paid_via=None, origin=None, by=None):
        intent = services.initiate_collection(
            entity=entity, amount=amount, customer=customer, invoice=invoice,
            origin=origin or CollectionOrigin.STANDARD, actor_user=by,
        )
        intent.created_by = by
        intent.save(update_fields=["created_by"])
        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        if paid_via:
            self.fake.forced_paid_via[intent.reference] = paid_via
        return services.confirm_collection(intent)

    def test_a_confirmed_collection_records_how_it_was_paid(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=5_000_000)
        card = self.pay(entity, customer, invoice, 2_000_000)
        ussd = self.pay(entity, customer, invoice, 1_000_000, paid_via=PaidVia.USSD)
        self.assertEqual(card.paid_via, PaidVia.CARD)
        self.assertEqual(ussd.paid_via, PaidVia.USSD)

    def test_a_receipt_keeps_the_balance_it_left_after_later_payments(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=5_000_000)
        first = self.pay(entity, customer, invoice, 2_000_000)
        self.pay(entity, customer, invoice, 1_000_000)

        allocation = PaymentAllocation.objects.get(payment_id=first.payment_id)
        self.assertEqual(allocation.invoice_balance_after, 3_000_000)
        payment = Payment.objects.get(pk=first.payment_id)
        self.assertEqual(payment.customer_balance_after, 3_000_000)

        context = receipt_document_context(payment)
        self.assertEqual(context["receipt"]["allocations"][0]["invoice_balance_after"], "₦30,000.00")
        self.assertEqual(context["receipt"]["customer_balance_after"], "₦30,000.00 owing")

    def test_a_receipt_from_before_the_balance_was_recorded_reads_todays(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=5_000_000)
        first = self.pay(entity, customer, invoice, 2_000_000)
        PaymentAllocation.objects.filter(payment_id=first.payment_id).update(
            invoice_balance_after=None,
        )
        context = receipt_document_context(Payment.objects.get(pk=first.payment_id))
        self.assertEqual(context["receipt"]["allocations"][0]["invoice_balance_after"], "₦30,000.00")

    def test_a_portal_payment_tells_the_account_that_started_it(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=5_000_000)
        payer = User.objects.create_user(
            "payer@acme.test", tenant=entity.tenant, first_name="Ada", last_name="Okoye",
        )
        with mock.patch(SEND) as send, self.captureOnCommitCallbacks(execute=True):
            self.pay(
                entity, customer, invoice, 2_000_000,
                origin=CollectionOrigin.PORTAL, by=payer,
            )
        told = [
            call for call in send.call_args_list
            if call.kwargs.get("recipients") == [payer]
        ]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0].kwargs["event_key"], "billing.payment_received")

    def test_a_collection_staff_started_does_not_tell_them(self):
        entity, customer, _ = self.build()
        invoice = self.make_posted_invoice(entity, customer, amount=5_000_000)
        bursar = User.objects.create_user(
            "bursar@acme.test", tenant=entity.tenant, first_name="Bisi", last_name="Bursar",
        )
        with mock.patch(SEND) as send, self.captureOnCommitCallbacks(execute=True):
            self.pay(entity, customer, invoice, 2_000_000, by=bursar)
        self.assertFalse(
            [c for c in send.call_args_list if c.kwargs.get("recipients") == [bursar]],
        )
