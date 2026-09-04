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
from unittest import mock

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
from .models import Account, Invoice, Payment
from .pay_links import (
    PLATFORM_PAY_SUBDOMAIN,
    InvoiceNotPayableError,
    invoice_from_token,
    invoice_pay_url,
    make_invoice_pay_token,
    payer_base_url,
    payer_return_url,
    start_checkout,
    summary,
)
from .receivables import post_invoice, post_payment
from .views_public import InvoicePayLinkThrottle
from .tests import _ARFixtureMixin

# The Console. Right for the links STAFF get, and wrong for a paying customer -
# these tests exist partly to keep those two apart.
STAGING = "https://intranet.codexng.com"
# The school app. A school's payer lands on <slug>.SCHOOL_APP; a platform
# invoice's payer lands on SCHOOL_APP itself.
SCHOOL_APP = "https://xvs.codexng.com"


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

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING)
    def test_url_points_at_the_school_app_never_the_console(self):
        # These books have no school, so this is the platform fallback. What
        # matters either way is that it is not the Console: FRONTEND_BASE_URL is
        # set to the Console here and must not leak into a customer's link.
        _entity, _period, _customer, invoice = self.build_payable()
        url = invoice_pay_url(invoice)

        self.assertTrue(url.startswith("https://pay.xvs.codexng.com/pay/"))
        self.assertNotIn("intranet", url)
        self.assertNotIn("localhost", url)

    @override_settings(SCHOOL_APP_BASE_URL="", PLATFORM_PAY_BASE_URL="")
    def test_no_school_app_configured_yields_no_link(self):
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

    @override_settings(
        SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="",
    )
    def test_button_is_the_pay_page_not_the_return_url(self):
        _entity, _period, _customer, invoice = self.build_payable()
        link = _invoice_context(invoice, self._Delivery())["payment_link"]

        self.assertTrue(link.startswith("https://pay.xvs.codexng.com/pay/"))
        # The three things it has been, and must never be again: the Console,
        # a localhost address, and the post-payment return page.
        self.assertNotIn("intranet", link)
        self.assertNotIn("localhost", link)
        self.assertNotIn("/payments/return", link)
        self.assertEqual(invoice_from_token(link.rsplit("/", 1)[-1]).pk, invoice.pk)


# --------------------------------------------------------------------------- #
# The public pay page                                                          #
# --------------------------------------------------------------------------- #

@override_settings(
    SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="",
)
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

    def test_summary_says_whether_the_platform_is_the_one_billing(self):
        # The page draws the XVS mark for a CodeX invoice and the school's own
        # name for a school's. It needs to be told which; the issuer name cannot
        # answer it, because that name is configurable.
        _entity, _period, _customer, invoice = self.build_payable()
        data = self.client.get(self.url(invoice)).json()["data"]

        self.assertIn("issuer_is_platform", data)
        self.assertIs(data["issuer_is_platform"], invoice.entity.is_platform)

    def test_no_logo_configured_means_no_logo_url(self):
        # The page falls back to the issuer's name in words, so an empty string
        # here is a real answer rather than a missing one.
        _entity, _period, _customer, invoice = self.build_payable()
        self.assertEqual(self.client.get(self.url(invoice)).json()["data"]["logo_url"], "")

    def test_the_logo_route_404s_when_there_is_no_logo(self):
        _entity, _period, _customer, invoice = self.build_payable()
        response = self.client.get(
            reverse("public-invoice-logo", args=[make_invoice_pay_token(invoice)]),
        )
        self.assertEqual(response.status_code, 404)

    def test_the_logo_route_refuses_a_forged_token(self):
        response = self.client.get(reverse("public-invoice-logo", args=["forged"]))
        self.assertEqual(response.status_code, 404)

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

    def test_the_psp_is_told_to_return_the_payer_to_the_school_app(self):
        # Not the Console. A parent finishing at a backoffice they cannot open
        # would reasonably think the money went somewhere it did not.
        _entity, _period, _customer, invoice = self.build_payable()
        self.client.post(self.checkout_url(invoice))
        self.assertEqual(
            self.provider.callbacks, ["https://pay.xvs.codexng.com/payments/return"],
        )

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

        The rate is stated here rather than read from the running settings. DRF
        binds ``THROTTLE_RATES`` onto the throttle class at import, so patching the
        class is the only thing that reaches it, and a settings module is free to
        change or drop the deployed rate without this test's meaning changing. What
        it asserts is that a link has a budget of its own; three requests show that
        as well as twelve, and in a quarter of the time.
        """
        entity, _period, customer, first = self.build_payable()
        second = self.make_invoice(entity, customer, lines=[("4100", 1, 90000, None)])
        post_invoice(second)

        rate = 3
        with mock.patch.object(
            InvoicePayLinkThrottle, "THROTTLE_RATES",
            {"invoice_pay_link": f"{rate}/hour"},
        ):
            statuses = [self.client.post(self.checkout_url(first)).status_code
                        for _ in range(rate + 1)]
            self.assertEqual(statuses[:rate], [200] * rate)
            self.assertEqual(statuses[-1], 429)

            # The other invoice's link is untouched by that.
            self.assertEqual(
                self.client.post(self.checkout_url(second)).status_code, 200,
            )

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


# --------------------------------------------------------------------------- #
# A school's own crest                                                         #
# --------------------------------------------------------------------------- #

@override_settings(FRONTEND_BASE_URL=STAGING, PAYMENTS_CALLBACK_URL="")
class SchoolLogoOnThePayPageTests(TestCase):
    """A parent paying school fees should see their school's badge.

    The ordinary signed media URL cannot serve them: it is bound to a reader and
    the payer has no session. So the crest comes back from a public route that
    takes the file from the invoice rather than from the caller, which is what
    lets the route be public at all.

    Builds its own entities rather than reusing the shared AR fixture, because
    ``LedgerEntity.code`` is globally unique and these tests need two schools.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def build_school_invoice(self, *, slug="corona", code="CORONA",
                             name="Corona Secondary School", logo=b"", total=180000):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from schools.vs_schools.models import School, SchoolBranding

        from .models import Customer, FiscalPeriod, FiscalYear, LedgerEntity
        from .constants import PeriodStatus
        from .seed import seed_chart_of_accounts, seed_currencies

        school = School.objects.create(name=name, slug=slug, status="ACTIVE")
        branding = SchoolBranding(school=school)
        if logo:
            branding.logo = SimpleUploadedFile(
                f"{slug}-crest.png", logo, content_type="image/png",
            )
        branding.save()

        seed_currencies()
        entity = LedgerEntity.objects.create(
            name=name, code=code, kind=LedgerEntity.Kind.TENANT, tenant=school.tenant,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        customer = Customer.objects.create(
            entity=entity, code=f"C-{code}", name="Mrs Adeyemi",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
            billing_email="adeyemi@example.com",
        )
        invoice = self._invoice(entity, customer, total)
        return school, invoice

    def _invoice(self, entity, customer, total):
        from .models import Invoice, InvoiceLine

        invoice = Invoice.objects.create(
            entity=entity, customer=customer,
            invoice_date=datetime.date(2026, 1, 5), due_date=datetime.date(2026, 1, 30),
        )
        InvoiceLine.objects.create(
            invoice=invoice,
            revenue_account=Account.objects.get(entity=entity, code="4100"),
            quantity=1, unit_price=total, tax_code=None, line_no=1,
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def summary_for(self, invoice):
        url = reverse("public-invoice-pay", args=[make_invoice_pay_token(invoice)])
        return self.client.get(url).json()["data"]

    def logo_response(self, invoice):
        return self.client.get(
            reverse("public-invoice-logo", args=[make_invoice_pay_token(invoice)]),
        )

    PNG = b"\x89PNG\r\n\x1a\n-corona-crest"

    def test_the_school_is_the_issuer_not_the_platform(self):
        # Which is what tells the page to draw the crest rather than the XVS mark.
        _school, invoice = self.build_school_invoice(logo=self.PNG)
        data = self.summary_for(invoice)

        self.assertFalse(data["issuer_is_platform"])
        self.assertEqual(data["issuer_name"], "Corona Secondary School")

    def test_the_logo_url_is_absolute(self):
        # The school app and the API are on different hosts, so a bare path
        # would resolve against the app's own origin and fetch nothing.
        _school, invoice = self.build_school_invoice(logo=self.PNG)
        logo_url = self.summary_for(invoice)["logo_url"]

        self.assertTrue(logo_url.startswith("http://"), logo_url)
        self.assertIn("/finance/public/invoices/", logo_url)
        self.assertTrue(logo_url.endswith("/logo/"), logo_url)

    def test_the_route_serves_the_crest_to_a_caller_with_no_session(self):
        _school, invoice = self.build_school_invoice(logo=self.PNG)
        response = self.logo_response(invoice)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("image/"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        # One school's crest answered to one link holder: never a shared cache.
        self.assertIn("private", response["Cache-Control"])
        self.assertEqual(response.content, self.PNG)

    def test_a_school_without_a_logo_falls_back_to_its_name(self):
        _school, invoice = self.build_school_invoice(logo=b"")
        data = self.summary_for(invoice)

        self.assertEqual(data["logo_url"], "")
        self.assertEqual(data["issuer_name"], "Corona Secondary School")
        self.assertEqual(self.logo_response(invoice).status_code, 404)

    def test_one_school_token_never_serves_another_school_crest(self):
        # The bytes are chosen by the invoice, never named by the caller. Two
        # schools, two crests, and neither link can reach the other's file.
        _corona, corona_invoice = self.build_school_invoice(logo=self.PNG)
        other_png = b"\x89PNG\r\n\x1a\n-bright-star-crest"
        _bright, bright_invoice = self.build_school_invoice(
            slug="bright-star", code="BRIGHT", name="Bright Star School", logo=other_png,
        )

        self.assertEqual(self.logo_response(corona_invoice).content, self.PNG)
        self.assertEqual(self.logo_response(bright_invoice).content, other_png)


# --------------------------------------------------------------------------- #
# Which host a payer belongs on                                                #
# --------------------------------------------------------------------------- #

class PayerHostTests(TestCase):
    """A paying customer goes to their own school's app, never to the Console.

    ``FRONTEND_BASE_URL`` addresses the Console, where Codex staff sign in. It is
    the right base for an invitation or a password reset and the wrong one for a
    parent, who has no account anywhere and belongs on their school's own
    address. Every case here sets it to the Console deliberately, so a value
    leaking back into a customer link fails loudly.

    Stubs stand in for the ORM: what is under test is the mapping from a tenant
    to a host, and building four schools to assert four strings would test
    Django's foreign keys instead.
    """

    class _Invoice:
        def __init__(self, slug=None):
            profile = object() if slug else None
            tenant = None if slug is None else type(
                "T", (), {"slug": slug, "school_profile": profile},
            )()
            self.entity = type("E", (), {"tenant": tenant})()

    def school(self, slug="corona"):
        return self._Invoice(slug)

    def platform(self):
        return self._Invoice(None)

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING)
    def test_a_school_payer_gets_that_school_subdomain(self):
        self.assertEqual(
            payer_base_url(self.school("corona")), "https://corona.xvs.codexng.com",
        )

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING)
    def test_each_school_gets_its_own(self):
        # One setting, every tenant. A new school needs no configuration at all.
        self.assertEqual(
            payer_base_url(self.school("bright-star")),
            "https://bright-star.xvs.codexng.com",
        )

    @override_settings(SCHOOL_APP_BASE_URL="http://localhost:5174")
    def test_the_same_shape_works_locally(self):
        # <slug>.localhost resolves to 127.0.0.1 with no hosts-file entry, which
        # is the shape the onboarding seeder already prints.
        self.assertEqual(
            payer_base_url(self.school("corona")), "http://corona.localhost:5174",
        )

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP, PLATFORM_PAY_BASE_URL="")
    def test_a_platform_invoice_uses_the_reserved_pay_subdomain(self):
        # CodeX billing a school has no school subdomain to build from: the books
        # belong to the platform tenant and a Customer records no tenant at all.
        # It must still be a SUBDOMAIN, because the bare product host serves
        # marketing rather than the app - a link there reaches nothing payable.
        self.assertEqual(payer_base_url(self.platform()), "https://pay.xvs.codexng.com")
        self.assertNotEqual(payer_base_url(self.platform()), SCHOOL_APP)

    def test_the_platform_label_can_never_be_claimed_by_a_school(self):
        # Otherwise a school registering the slug "pay" would quietly take over
        # the host every platform invoice points at.
        from vs_tenants.models import RESERVED_TENANT_SLUGS

        self.assertIn(PLATFORM_PAY_SUBDOMAIN, RESERVED_TENANT_SLUGS)

    @override_settings(
        SCHOOL_APP_BASE_URL=SCHOOL_APP,
        PLATFORM_PAY_BASE_URL="https://billing.codexng.com",
    )
    def test_the_platform_host_is_an_env_var_not_a_code_decision(self):
        # Moving these payers is one environment change, not a deploy.
        self.assertEqual(payer_base_url(self.platform()), "https://billing.codexng.com")
        # And it must not drag a school's payer along with it.
        self.assertEqual(
            payer_base_url(self.school("corona")), "https://corona.xvs.codexng.com",
        )

    @override_settings(SCHOOL_APP_BASE_URL=f"{SCHOOL_APP}/", FRONTEND_BASE_URL=STAGING)
    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(
            payer_base_url(self.school("corona")), "https://corona.xvs.codexng.com",
        )

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP)
    def test_the_return_url_stays_on_the_payer_host(self):
        self.assertEqual(
            payer_return_url(self.school("corona")),
            "https://corona.xvs.codexng.com/payments/return",
        )

    @override_settings(SCHOOL_APP_BASE_URL="", PLATFORM_PAY_BASE_URL="")
    def test_nothing_configured_yields_nothing_rather_than_a_broken_host(self):
        self.assertEqual(payer_base_url(self.school("corona")), "")
        self.assertEqual(payer_return_url(self.school("corona")), "")


class SchoolPayerHostFromRealBooksTests(TestCase):
    """The same mapping, through an actual school tenant rather than a stub."""

    @override_settings(SCHOOL_APP_BASE_URL=SCHOOL_APP, FRONTEND_BASE_URL=STAGING)
    def test_a_school_owned_invoice_links_to_that_school(self):
        from schools.vs_schools.models import School

        from .models import Customer, LedgerEntity
        from .seed import seed_chart_of_accounts, seed_currencies

        school = School.objects.create(
            name="Corona Secondary School", slug="corona", status="ACTIVE",
        )
        seed_currencies()
        entity = LedgerEntity.objects.create(
            name=school.name, code="CORONA", kind=LedgerEntity.Kind.TENANT,
            tenant=school.tenant,
        )
        seed_chart_of_accounts(entity)
        customer = Customer.objects.create(
            entity=entity, code="C-1", name="Mrs Adeyemi",
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )
        invoice = Invoice.objects.create(
            entity=entity, customer=customer,
            invoice_date=datetime.date(2026, 1, 5), due_date=datetime.date(2026, 1, 30),
        )

        self.assertEqual(payer_base_url(invoice), "https://corona.xvs.codexng.com")
        self.assertTrue(
            invoice_pay_url(invoice).startswith("https://corona.xvs.codexng.com/pay/"),
        )


class PayLinkThrottleKeyTests(TestCase):
    """The per-link limit has to follow the LINK, not the string that spells it.

    ``signing.dumps`` stamps the time into every token, so asking for the same
    invoice's link twice gives two different strings. Keying the throttle on the
    string meant a resent invoice arrived with a fresh budget, and meant the
    limit applied or did not depending on which side of a second boundary two
    requests fell.
    """

    class _View:
        def __init__(self, token):
            self.kwargs = {"token": token}

    def key_for(self, token):
        return InvoicePayLinkThrottle().get_cache_key(None, self._View(token))

    def test_two_tokens_for_one_invoice_share_a_bucket(self):
        from .models import Invoice

        invoice = Invoice(pk=42, pay_token_version=1)
        first, second = make_invoice_pay_token(invoice), make_invoice_pay_token(invoice)

        self.assertEqual(self.key_for(first), self.key_for(second))
        self.assertIsNotNone(self.key_for(first))

    def test_two_invoices_do_not(self):
        from .models import Invoice

        self.assertNotEqual(
            self.key_for(make_invoice_pay_token(Invoice(pk=42, pay_token_version=1))),
            self.key_for(make_invoice_pay_token(Invoice(pk=43, pay_token_version=1))),
        )

    def test_revoking_a_link_starts_a_new_bucket(self):
        # A revoked link and its replacement are different links, and the spent
        # budget of the old one must not follow the payer to the new one.
        from .models import Invoice

        self.assertNotEqual(
            self.key_for(make_invoice_pay_token(Invoice(pk=42, pay_token_version=1))),
            self.key_for(make_invoice_pay_token(Invoice(pk=42, pay_token_version=2))),
        )

    def test_a_forged_token_gets_no_bucket_of_its_own(self):
        # It names no link. The IP-scoped throttle is what bounds that traffic;
        # giving it a per-link budget would let a guesser mint unlimited ones.
        for token in ("", "forged", "not.a.token"):
            with self.subTest(token=token):
                self.assertIsNone(self.key_for(token))
