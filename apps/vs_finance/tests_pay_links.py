"""The invoice pay link, and the settings freeze that made it point at localhost.

Two defects are covered here, and they are separate.

The visible one: the "Pay online" button in an invoice email carried
``settings.PAYMENTS_CALLBACK_URL`` - the URL a payer *returns to after paying* - so it
was never a way to pay at all. It now carries a signed link to a public pay page that
mints the checkout when the payer clicks, for the balance outstanding at that moment.

The one underneath it: ``PAYMENTS_CALLBACK_URL`` was defaulted in ``base.py`` from an
f-string over ``FRONTEND_BASE_URL``, which the environment modules set only *after*
importing base. The default therefore froze at ``http://localhost:3000`` and staging
sent that to real customers. These tests pin the behaviour that would have caught it:
the callback follows whatever frontend the running environment actually configured.
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from vs_payments.constants import CollectionStatus
from vs_payments.models import CollectionIntent
from vs_payments.providers import registry
from vs_payments.providers.fake import FakeProvider
from vs_payments.services import default_callback_url, initiate_collection

from .constants import DocumentStatus
from .document_email import _invoice_context
from .models import Account, Payment
from .pay_links import (
    InvoiceNotPayableError,
    invoice_from_token,
    invoice_pay_url,
    make_invoice_pay_token,
    start_checkout,
    summary,
)
from .receivables import post_invoice, post_payment
from .tests import _ARFixtureMixin

STAGING = "https://intranet.codexng.com"


class _RecordingProvider(FakeProvider):
    """A fake that remembers the callback URL it was handed."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.callbacks: list[str] = []

    def create_checkout(self, **kwargs):
        self.callbacks.append(kwargs.get("callback_url", ""))
        return super().create_checkout(**kwargs)


class _PayLinkFixture(_ARFixtureMixin):
    """A posted invoice for ₦1,800.00 with a fake PSP wired in."""

    def build_payable(self, *, total=180000):
        entity, period, customer, _vat = self.build_ar()
        customer.billing_email = "adeyemi@example.com"
        customer.billing_phone = "08030000000"
        customer.billing_address = "12 Bourdillon Road, Ikoyi"
        customer.save()
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, total, None)],
            date=datetime.date(2026, 1, 5), due=datetime.date(2026, 1, 30),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()

        self.provider = _RecordingProvider(secret="test-secret")
        registry.register("PAYSTACK", self.provider)
        registry.register("FAKE", self.provider)
        self.addCleanup(registry.unregister)
        return entity, period, customer, invoice

    def pay(self, entity, customer, amount, *, date=datetime.date(2026, 1, 12)):
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=date, amount=amount,
            deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        return payment


# --------------------------------------------------------------------------- #
# The settings freeze                                                          #
# --------------------------------------------------------------------------- #

class CallbackUrlResolutionTests(TestCase):
    """The return URL must track the environment, not base.py's import-time default."""

    @override_settings(FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="")
    def test_callback_follows_the_configured_frontend(self):
        # The regression in one line: staging sets FRONTEND_BASE_URL after importing
        # base, so anything derived at import time would still say localhost here.
        self.assertEqual(default_callback_url(), f"{STAGING}/payments/return")
        self.assertNotIn("localhost", default_callback_url())

    @override_settings(FRONTEND_BASE_URL="http://localhost:5173", PAYMENTS_CALLBACK_URL="")
    def test_callback_follows_a_local_frontend_too(self):
        # Same call, different environment, different answer - which is the point.
        self.assertEqual(default_callback_url(), "http://localhost:5173/payments/return")

    @override_settings(
        FRONTEND_BASE_URL=STAGING,
        PAYMENTS_CALLBACK_URL="https://pay.codexng.com/done",
    )
    def test_an_explicit_callback_wins(self):
        self.assertEqual(default_callback_url(), "https://pay.codexng.com/done")

    @override_settings(FRONTEND_BASE_URL="", PAYMENTS_CALLBACK_URL="")
    def test_no_frontend_configured_yields_no_callback(self):
        self.assertEqual(default_callback_url(), "")

    @override_settings(FRONTEND_BASE_URL=f"{STAGING}/", PAYMENTS_CALLBACK_URL="")
    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(default_callback_url(), f"{STAGING}/payments/return")


class CallbackReachesTheProviderTests(_PayLinkFixture, TestCase):
    """What the PSP is actually told, which is the thing the customer ends up seeing."""

    @override_settings(FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="")
    def test_provider_receives_the_environment_frontend(self):
        entity, _period, customer, invoice = self.build_payable()
        initiate_collection(
            entity=entity, amount=invoice.balance_due, customer=customer, invoice=invoice,
        )
        self.assertEqual(self.provider.callbacks, [f"{STAGING}/payments/return"])


# --------------------------------------------------------------------------- #
# Token                                                                        #
# --------------------------------------------------------------------------- #

class InvoicePayTokenTests(_PayLinkFixture, TestCase):

    @override_settings(FRONTEND_BASE_URL=STAGING)
    def test_round_trip(self):
        _entity, _period, _customer, invoice = self.build_payable()
        self.assertEqual(invoice_from_token(make_invoice_pay_token(invoice)).pk, invoice.pk)

    @override_settings(FRONTEND_BASE_URL=STAGING)
    def test_url_points_at_the_configured_frontend(self):
        _entity, _period, _customer, invoice = self.build_payable()
        url = invoice_pay_url(invoice)
        self.assertTrue(url.startswith(f"{STAGING}/pay/"))
        self.assertNotIn("localhost", url)

    @override_settings(FRONTEND_BASE_URL="")
    def test_no_frontend_configured_yields_no_link(self):
        _entity, _period, _customer, invoice = self.build_payable()
        self.assertEqual(invoice_pay_url(invoice), "")

    @override_settings(FRONTEND_BASE_URL=STAGING)
    def test_a_tampered_token_is_refused(self):
        _entity, _period, _customer, invoice = self.build_payable()
        token = make_invoice_pay_token(invoice)
        for forged in (token[:-1] + ("A" if token[-1] != "A" else "B"), "", "not-a-token"):
            with self.subTest(token=forged):
                with self.assertRaises(NotFound):
                    invoice_from_token(forged)

    @override_settings(FRONTEND_BASE_URL=STAGING)
    def test_a_token_never_resolves_to_another_invoice(self):
        entity, _period, customer, first = self.build_payable()
        second = self.make_invoice(entity, customer, lines=[("4100", 1, 5000, None)])
        post_invoice(second)
        self.assertEqual(invoice_from_token(make_invoice_pay_token(first)).pk, first.pk)
        self.assertEqual(invoice_from_token(make_invoice_pay_token(second)).pk, second.pk)


# --------------------------------------------------------------------------- #
# The email                                                                    #
# --------------------------------------------------------------------------- #

class InvoiceEmailPaymentLinkTests(_PayLinkFixture, TestCase):
    """The button in the email a customer receives."""

    class _Delivery:
        note = ""

    @override_settings(FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="")
    def test_button_is_the_pay_page_not_the_return_url(self):
        _entity, _period, _customer, invoice = self.build_payable()
        link = _invoice_context(invoice, self._Delivery())["payment_link"]

        self.assertTrue(link.startswith(f"{STAGING}/pay/"))
        # The two things it used to be, and must never be again.
        self.assertNotIn("localhost", link)
        self.assertNotIn("/payments/return", link)
        self.assertEqual(invoice_from_token(link.rsplit("/", 1)[-1]).pk, invoice.pk)


# --------------------------------------------------------------------------- #
# The public pay page                                                          #
# --------------------------------------------------------------------------- #

@override_settings(FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="")
class PublicInvoicePayPageTests(_PayLinkFixture, TestCase):

    def setUp(self):
        # Both routes are throttled by IP, and every test here shares 127.0.0.1.
        # Without this the counters carry over and a later test 429s on a request
        # that has nothing wrong with it.
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def url(self, invoice):
        return reverse("public-invoice-pay", args=[make_invoice_pay_token(invoice)])

    def checkout_url(self, invoice):
        return reverse("public-invoice-checkout", args=[make_invoice_pay_token(invoice)])

    # -- reading -------------------------------------------------------------- #

    def test_anonymous_caller_can_read_the_invoice(self):
        _entity, _period, _customer, invoice = self.build_payable()
        response = self.client.get(self.url(invoice))

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["payable"])
        self.assertEqual(data["amount_due_kobo"], 180000)
        self.assertEqual(data["amount_due"], "₦1,800.00")
        self.assertEqual(data["invoice_number"], invoice.document_number)

    def test_summary_does_not_leak_the_customer_contact_record(self):
        # The document number, the amount and the customer's name already went to
        # this address on the invoice itself. The address book did not.
        _entity, _period, _customer, invoice = self.build_payable()
        data = self.client.get(self.url(invoice)).json()["data"]

        body = str(data)
        self.assertNotIn("adeyemi@example.com", body)
        self.assertNotIn("08030000000", body)
        self.assertNotIn("Bourdillon", body)

    def test_a_forged_token_is_a_404(self):
        _entity, _period, _customer, invoice = self.build_payable()
        response = self.client.get(reverse("public-invoice-pay", args=["forged"]))
        self.assertEqual(response.status_code, 404)

    # -- the amount is read at click time ------------------------------------- #

    def test_part_payment_shrinks_the_amount_before_the_payer_clicks(self):
        """The defect this whole change exists for.

        An invoice for ₦1,800.00 goes out on the 5th. The customer pays ₦1,000.00 by
        bank transfer on the 12th. When they open the same email on the 20th, the
        link must ask for the ₦800.00 still outstanding - not the ₦1,800.00 that was
        true when the email was written.
        """
        entity, _period, customer, invoice = self.build_payable()
        self.pay(entity, customer, 100000)

        data = self.client.get(self.url(invoice)).json()["data"]
        self.assertEqual(data["amount_due_kobo"], 80000)
        self.assertEqual(data["amount_due"], "₦800.00")

        started = self.client.post(self.checkout_url(invoice)).json()["data"]
        self.assertEqual(started["amount_kobo"], 80000)

        intent = CollectionIntent.objects.get(invoice=invoice)
        self.assertEqual(intent.amount, 80000)

    # -- starting a checkout --------------------------------------------------- #

    def test_checkout_returns_the_provider_url_and_records_the_intent(self):
        _entity, _period, _customer, invoice = self.build_payable()
        response = self.client.post(self.checkout_url(invoice))

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        intent = CollectionIntent.objects.get(invoice=invoice)
        self.assertEqual(data["checkout_url"], intent.checkout_url)
        self.assertTrue(data["checkout_url"])
        self.assertEqual(data["reference"], intent.reference)
        self.assertEqual(intent.amount, 180000)
        self.assertEqual(intent.customer_id, invoice.customer_id)
        self.assertEqual(intent.status, CollectionStatus.PROCESSING)

    def test_the_psp_is_told_the_real_return_url(self):
        _entity, _period, _customer, invoice = self.build_payable()
        self.client.post(self.checkout_url(invoice))
        self.assertEqual(self.provider.callbacks, [f"{STAGING}/payments/return"])

    def test_a_second_click_reuses_the_open_checkout(self):
        # Otherwise a refresh leaves two live ways to pay the same money.
        _entity, _period, _customer, invoice = self.build_payable()
        first = self.client.post(self.checkout_url(invoice)).json()["data"]
        second = self.client.post(self.checkout_url(invoice)).json()["data"]

        self.assertEqual(first["reference"], second["reference"])
        self.assertEqual(CollectionIntent.objects.filter(invoice=invoice).count(), 1)
        self.assertEqual(len(self.provider.callbacks), 1)

    # -- rate limiting ---------------------------------------------------------

    def test_one_link_running_out_does_not_block_another_payer(self):
        """A whole school's parents can share one IP, so the limit is per link.

        Corona Secondary School sends fee invoices to Mrs Adeyemi and Mr Okonkwo.
        Both pay from the school office wifi during the fee drive, so both arrive on
        the same address. Mrs Adeyemi's browser retries her link until it is spent;
        Mr Okonkwo must still be able to pay his.
        """
        entity, _period, customer, first = self.build_payable()
        second = self.make_invoice(entity, customer, lines=[("4100", 1, 90000, None)])
        post_invoice(second)

        rate = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["invoice_pay_link"]
                   .split("/")[0])
        statuses = [self.client.post(self.checkout_url(first)).status_code
                    for _ in range(rate + 1)]
        self.assertEqual(statuses[:rate], [200] * rate)
        self.assertEqual(statuses[-1], 429)

        # The other invoice's link is untouched by that.
        self.assertEqual(self.client.post(self.checkout_url(second)).status_code, 200)

    # -- invoices that cannot take a payment ----------------------------------- #

    def test_a_settled_invoice_says_so_instead_of_charging_again(self):
        entity, _period, customer, invoice = self.build_payable()
        self.pay(entity, customer, 180000)

        data = self.client.get(self.url(invoice)).json()["data"]
        self.assertFalse(data["payable"])
        self.assertIn("paid in full", data["message"])
        self.assertEqual(data["amount_due_kobo"], 0)

        response = self.client.post(self.checkout_url(invoice))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "INVOICE_NOT_PAYABLE")
        self.assertFalse(CollectionIntent.objects.filter(invoice=invoice).exists())

    def test_a_cancelled_invoice_cannot_be_paid(self):
        _entity, _period, _customer, invoice = self.build_payable()
        invoice.status = DocumentStatus.CANCELLED
        invoice.save(update_fields=["status"])

        data = self.client.get(self.url(invoice)).json()["data"]
        self.assertFalse(data["payable"])
        self.assertIn("cancelled", data["message"].lower())
        self.assertEqual(self.client.post(self.checkout_url(invoice)).status_code, 409)
        self.assertFalse(CollectionIntent.objects.filter(invoice=invoice).exists())

    def test_a_draft_invoice_cannot_be_paid(self):
        entity, _period, customer, _invoice = self.build_payable()
        draft = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])

        data = self.client.get(self.url(draft)).json()["data"]
        self.assertFalse(data["payable"])
        self.assertEqual(self.client.post(self.checkout_url(draft)).status_code, 409)
        self.assertFalse(CollectionIntent.objects.filter(invoice=draft).exists())

    def test_a_credited_invoice_is_not_thanked_for_paying(self):
        # Balance cleared by a credit note, not by money. Saying "paid in full,
        # thank you" to a parent whose fees were waived is simply untrue.
        _entity, _period, _customer, invoice = self.build_payable()
        invoice.amount_credited = invoice.total
        invoice.save(update_fields=["amount_credited"])

        data = self.client.get(self.url(invoice)).json()["data"]
        self.assertFalse(data["payable"])
        self.assertEqual(data["message"], "There is nothing left to pay on this invoice.")
        self.assertEqual(self.client.post(self.checkout_url(invoice)).status_code, 409)

    def test_start_checkout_refuses_a_settled_invoice_at_the_service_layer(self):
        # The view is not the only caller, so the rule lives below it.
        entity, _period, customer, invoice = self.build_payable()
        self.pay(entity, customer, 180000)
        with self.assertRaises(InvoiceNotPayableError):
            start_checkout(make_invoice_pay_token(invoice))

    def test_summary_reads_a_draft_without_raising(self):
        entity, _period, customer, _invoice = self.build_payable()
        draft = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        self.assertFalse(summary(make_invoice_pay_token(draft))["payable"])
