"""How the books' owner asks a customer to pay, with no school anywhere in sight.

A product registers a provider with :mod:`vs_finance.payer_options` and answers
per customer. These tests use a stand-in provider and the engine's own
non-school books, so they prove the engine's half on its own: with no answer
(or a provider that fails) everything behaves as it always has, and with an
answer that asks for bank transfer the pay link refuses a checkout, the invoice
prints the account instead of the pay-online block, and the invoice email
names the account instead of the link.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from vs_payments.models import CollectionIntent

from .document_email import _invoice_context
from .documents import (
    _withhold_payer_transfer,
    invoice_document_context,
    render_invoice_document_html,
)
from .pay_links import (
    CardPaymentsOffError,
    make_invoice_pay_token,
    start_checkout,
    summary,
)
from .payer_options import (
    TRANSFER_MATCHED_LINE,
    PayerPaymentOptions,
    PayerTransferAccount,
    card_payments_allowed,
    payer_payment_options,
    register_payer_options_provider,
)
from .tests_pay_links import SCHOOL_APP, STAGING, _PayLinkFixture

ACCOUNT = PayerTransferAccount(
    bank_name="Wema Bank", account_number="7001234567", account_name="Tunde Adeyemi",
)
TRANSFER_ONLY = PayerPaymentOptions(
    card_allowed=False, transfer_account=ACCOUNT,
    card_off_message="Pay by transfer, please.", no_account_message="Ask the office.",
)
NO_ACCOUNT_YET = PayerPaymentOptions(
    card_allowed=False, card_off_message="Pay by transfer, please.",
    no_account_message="Ask the office for your account number.",
)


class _Delivery:
    note = ""


def answering(options):
    """Patch the registered providers to one that answers *options* for every customer."""
    return mock.patch("vs_finance.payer_options._PROVIDERS", [lambda customer: options])


@override_settings(
    SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="",
)
class PayerOptionsTests(_PayLinkFixture, TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        self.entity, _period, self.customer, self.invoice = self.build_payable()

    def checkout(self):
        token = make_invoice_pay_token(self.invoice)
        return self.client.post(reverse("public-invoice-checkout", args=[token]))

    # -- no answer: today's behaviour ---------------------------------------- #

    def test_with_no_provider_a_customer_pays_by_card_as_before(self):
        with answering(None):
            self.assertIsNone(payer_payment_options(self.customer))
            self.assertTrue(card_payments_allowed(self.customer))
            self.assertEqual(self.checkout().status_code, 200)
            context = _invoice_context(self.invoice, _Delivery())
            self.assertTrue(context["payment_link"].startswith("https://pay.xvs.codexng.com/pay/"))
            self.assertEqual(context["payment_instructions"], f"Pay online: {context['payment_link']}")
            document = invoice_document_context(self.invoice)["invoice"]
            self.assertTrue(document["pay_url"])
            self.assertTrue(document["qr_image"])
            self.assertIsNone(document["payer_transfer"])
            self.assertEqual(document["payer_note"], "")

    def test_a_provider_that_fails_is_passed_over(self):
        def broken(customer):
            raise RuntimeError("provider down")

        with mock.patch("vs_finance.payer_options._PROVIDERS", [broken]):
            with self.assertLogs("vs_finance.payer_options", "ERROR"):
                self.assertTrue(card_payments_allowed(self.customer))
            self.assertEqual(self.checkout().status_code, 200)

    def test_the_first_provider_with_an_opinion_wins(self):
        providers = [lambda customer: None, lambda customer: TRANSFER_ONLY, lambda customer: None]
        with mock.patch("vs_finance.payer_options._PROVIDERS", providers):
            self.assertIs(payer_payment_options(self.customer), TRANSFER_ONLY)

    def test_registering_a_provider_twice_is_one(self):
        def provider(customer):
            return None

        with mock.patch("vs_finance.payer_options._PROVIDERS", []) as providers:
            register_payer_options_provider(provider)
            register_payer_options_provider(provider)
            self.assertEqual(len(providers), 1)

    # -- the pay link --------------------------------------------------------- #

    def test_the_pay_link_refuses_a_checkout_and_starts_nothing(self):
        with answering(TRANSFER_ONLY):
            response = self.checkout()
            with self.assertRaises(CardPaymentsOffError):
                start_checkout(make_invoice_pay_token(self.invoice))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CARD_PAYMENTS_OFF")
        self.assertEqual(response.json()["message"], "Pay by transfer, please.")
        self.assertFalse(CollectionIntent.objects.filter(invoice=self.invoice).exists())

    def test_the_pay_page_still_shows_the_balance_but_offers_no_payment(self):
        with answering(TRANSFER_ONLY):
            token = make_invoice_pay_token(self.invoice)
            data = self.client.get(reverse("public-invoice-pay", args=[token])).json()["data"]
        self.assertEqual(data["amount_due_kobo"], 180000)
        self.assertEqual(data["amount_due"], "₦1,800.00")
        self.assertFalse(data["payable"])
        self.assertFalse(data["card_payments"])
        self.assertEqual(data["message"], "Pay by transfer, please.")

    def test_the_pay_page_never_carries_the_account_number(self):
        with answering(TRANSFER_ONLY):
            data = summary(make_invoice_pay_token(self.invoice))
        self.assertNotIn(ACCOUNT.account_number, str(data))

    def test_a_card_payment_summary_says_card_is_offered(self):
        with answering(None):
            data = summary(make_invoice_pay_token(self.invoice))
        self.assertTrue(data["payable"])
        self.assertTrue(data["card_payments"])

    def test_a_paid_invoice_keeps_saying_it_is_paid(self):
        self.pay(self.entity, self.customer, self.invoice.balance_due)
        self.invoice.refresh_from_db()
        with answering(TRANSFER_ONLY):
            data = summary(make_invoice_pay_token(self.invoice))
        self.assertIn("paid in full", data["message"])

    # -- the invoice ---------------------------------------------------------- #

    def test_the_invoice_carries_the_account_in_place_of_the_pay_block(self):
        with answering(TRANSFER_ONLY):
            document = invoice_document_context(self.invoice)["invoice"]
            html = render_invoice_document_html(self.invoice)
        self.assertEqual(document["pay_url"], "")
        self.assertEqual(document["qr_image"], "")
        self.assertEqual(document["payer_transfer"]["account_number"], ACCOUNT.account_number)
        self.assertIn(ACCOUNT.account_number, html)
        self.assertIn("Wema Bank", html)
        self.assertIn("matched to this invoice automatically", html)
        self.assertNotIn("Or pay online", html)
        self.assertNotIn('<img class="qr-code"', html)

    def test_a_customer_with_no_account_yet_is_told_whom_to_ask(self):
        with answering(NO_ACCOUNT_YET):
            document = invoice_document_context(self.invoice)["invoice"]
            html = render_invoice_document_html(self.invoice)
        self.assertIsNone(document["payer_transfer"])
        self.assertEqual(document["payer_note"], "Ask the office for your account number.")
        self.assertIn("Ask the office for your account number.", html)
        self.assertNotIn("Or pay online", html)

    def test_a_reader_who_may_not_read_the_number_is_shown_no_account(self):
        denied = {"payments.virtual_account.account_number"}
        request = mock.Mock()
        with answering(TRANSFER_ONLY), mock.patch(
            "vs_rbac.field_enforcement.can_read", side_effect=lambda r, key: key not in denied,
        ):
            html = render_invoice_document_html(self.invoice, request=request)
        self.assertNotIn(ACCOUNT.account_number, html)
        self.assertNotIn("Wema Bank", html)

    def test_a_reader_who_may_not_read_the_name_is_shown_the_number_without_it(self):
        denied = {"payments.virtual_account.account_name"}
        block = {"payer_transfer": {"bank_name": "Wema Bank", "account_number": "7001234567",
                                    "account_name": "Tunde Adeyemi"}}
        with mock.patch(
            "vs_rbac.field_enforcement.can_read", side_effect=lambda r, key: key not in denied,
        ):
            _withhold_payer_transfer(block, mock.Mock())
        self.assertEqual(block["payer_transfer"]["account_number"], "7001234567")
        self.assertEqual(block["payer_transfer"]["account_name"], "")

    def test_a_render_for_nobody_keeps_everything(self):
        block = {"payer_transfer": {"bank_name": "Wema Bank", "account_number": "7001234567",
                                    "account_name": "Tunde Adeyemi"}}
        _withhold_payer_transfer(block, None)
        self.assertEqual(block["payer_transfer"]["account_name"], "Tunde Adeyemi")

    # -- the email ------------------------------------------------------------ #

    def test_the_email_names_the_account_and_carries_no_link(self):
        with answering(TRANSFER_ONLY):
            context = _invoice_context(self.invoice, _Delivery())
        self.assertEqual(context["payment_link"], "")
        self.assertIn("Wema Bank", context["payment_instructions"])
        self.assertIn("7001234567", context["payment_instructions"])
        self.assertIn("Tunde Adeyemi", context["payment_instructions"])
        self.assertIn(TRANSFER_MATCHED_LINE, context["payment_instructions"])
        self.assertNotIn("Pay online", context["payment_instructions"])

    def test_the_email_of_a_customer_with_no_account_says_so(self):
        with answering(NO_ACCOUNT_YET):
            context = _invoice_context(self.invoice, _Delivery())
        self.assertEqual(context["payment_link"], "")
        self.assertEqual(
            context["payment_instructions"], "Ask the office for your account number.",
        )
