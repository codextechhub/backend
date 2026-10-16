"""A pupil's own fees, payments and receipts in the portal, and paying.

Tunde is a pupil at Corona with a posted ₦3,000.00 tuition invoice; Bola, at
the same school, has one of her own. Corona's smallest part payment is the
default ₦1,000.00. Paying goes through the fake payment provider, which stands
in for Paystack: Tunde is handed a checkout, the provider later confirms it,
and his history and receipt follow.
"""
from __future__ import annotations

from unittest import mock

from django.test import override_settings
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.tests.base import FALFixture
from schools.core.fal.testing import unavailable_finance_reader
from schools.vs_students.services import portal_access
from vs_payments import services as payment_services
from vs_payments.constants import CollectionOrigin
from vs_payments.models import CollectionIntent
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider
from vs_user.models import User


def give_access(pupil, number):
    pupil.student_number = number
    pupil.email = f"pupil{pupil.pk}@example.ng"
    pupil.save(update_fields=["student_number", "email"])
    portal_access.issue(pupil, actor=None)
    pupil.refresh_from_db()
    User.objects.filter(pk=pupil.user_id).update(status=User.Status.ACTIVE, is_active=True)
    return User.objects.get(pk=pupil.user_id)


class PortalFeesTests(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        bridge.link_term(cls.structure.pk, cls.session.pk, cls.term.pk)

        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.bola = cls.student(cls.corona, cls.ikeja, first="Bola", last="Okafor")
        for pupil in (cls.tunde, cls.bola):
            cls.student_customer(cls.corona_books, str(pupil.pk), branch=cls.ikeja)
        bridge.generate_cohort_invoices(cls.structure.pk, (str(cls.tunde.pk), str(cls.bola.pk)))
        cls.tunde_invoice = Invoice.objects.get(customer__source_id=str(cls.tunde.pk))
        cls.bola_invoice = Invoice.objects.get(customer__source_id=str(cls.bola.pk))
        cls.tunde_user = give_access(cls.tunde, "COR/001")

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)
        self.addCleanup(fal_registry.reset)

    def as_pupil(self, user=None):
        return TenantAPIClient(user or self.tunde_user)

    def pay(self, amount, invoice=None):
        url = reverse("student-portal-pay", kwargs={"invoice_ref": (invoice or self.tunde_invoice).pk})
        return self.as_pupil().post(url, {"amount": amount}, format="json")

    # ── reading ──────────────────────────────────────────────────────────

    def test_the_pupil_reads_their_balance_and_invoice(self):
        data = self.as_pupil().get(reverse("student-portal-fees")).data["data"]
        self.assertEqual(data["balance"], 300_000)
        self.assertTrue(data["can_pay"])
        self.assertEqual(data["min_part_payment"], 100_000)
        invoice = data["invoices"][0]
        self.assertEqual(invoice["number"], self.tunde_invoice.document_number)
        self.assertEqual(invoice["status_label"], "Owing")
        self.assertEqual(invoice["lines"][0]["amount"], 300_000)

    def test_unreadable_books_say_so_rather_than_zero(self):
        fal_registry.set_finance_reader(unavailable_finance_reader())
        response = self.as_pupil().get(reverse("student-portal-fees"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "FINANCE_UNAVAILABLE")

    def test_staff_cannot_read_the_portal_fees(self):
        response = TenantAPIClient(self.bursar).get(reverse("student-portal-fees"))
        self.assertEqual(response.status_code, 404)

    # ── paying ───────────────────────────────────────────────────────────

    def test_a_part_payment_hands_back_a_checkout_started_by_the_pupil(self):
        response = self.pay(200_000)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["checkout_url"])
        self.assertEqual(response.data["data"]["balance_after"], 100_000)
        intent = CollectionIntent.objects.get(reference=response.data["data"]["reference"])
        self.assertEqual(intent.origin, CollectionOrigin.PORTAL)
        self.assertEqual(intent.created_by_id, self.tunde_user.pk)
        self.assertEqual(intent.invoice_id, self.tunde_invoice.pk)

    @override_settings(
        SCHOOL_APP_BASE_URL="https://xvs.codexng.com", STUDENT_APP_BASE_URL="",
    )
    def test_the_checkout_returns_the_pupil_to_the_student_apps_paid_page(self):
        with mock.patch.object(
            self.fake, "create_checkout", wraps=self.fake.create_checkout,
        ) as checkout:
            self.assertEqual(self.pay(200_000).status_code, 201)
        self.assertEqual(
            checkout.call_args.kwargs["callback_url"],
            f"https://{self.corona.tenant.slug}.xvs.codexng.com/student/fees/paid",
        )

    def test_the_whole_balance_may_always_be_paid(self):
        self.assertEqual(self.pay(300_000).status_code, 201)

    def test_a_part_payment_below_the_schools_minimum_is_refused(self):
        response = self.pay(50_000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_BELOW_MINIMUM")
        self.assertFalse(CollectionIntent.objects.exists())

    def test_more_than_the_balance_is_refused(self):
        response = self.pay(400_000)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_ABOVE_BALANCE")

    def test_another_pupils_invoice_is_not_found(self):
        response = self.pay(100_000, invoice=self.bola_invoice)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_school_that_turns_pupil_payments_off_refuses_them(self):
        from django.core.management import call_command
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key="fees.student_payment_enabled")
        set_value(definition=definition, value=False, actor=self.bursar, tenant=self.corona.tenant)

        response = self.pay(300_000)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "STUDENT_PAYMENT_OFF")
        self.assertFalse(self.as_pupil().get(reverse("student-portal-fees")).data["data"]["can_pay"])

    # ── after paying ─────────────────────────────────────────────────────

    def test_a_payment_waiting_for_the_provider_is_pending_with_no_receipt(self):
        self.pay(200_000)
        rows = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertFalse(rows[0]["has_receipt"])

    def test_a_confirmed_payment_has_a_receipt_with_the_balance_it_left(self):
        reference = self.pay(200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        payment_services.confirm_collection(intent)

        rows = self.as_pupil().get(reverse("student-portal-payments")).data["data"]
        row = rows[0]
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertEqual(row["method"], "CARD")
        self.assertEqual(row["payer_name"], self.tunde_user.full_name)

        url = reverse("student-portal-receipt", kwargs={"payment_ref": row["payment_ref"]})
        response = self.as_pupil().get(url)
        self.assertEqual(response.status_code, 200, response.data)
        receipt = response.data["data"]
        self.assertEqual(receipt["amount"], 200_000)
        self.assertEqual(receipt["lines"][0]["balance_after"], 100_000)
        pdf = self.as_pupil().get(url, {"as": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_another_pupils_receipt_is_not_found(self):
        reference = self.pay(200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        payment_services.confirm_collection(intent)
        intent.refresh_from_db()

        bola_user = give_access(self.bola, "COR/002")
        url = reverse("student-portal-receipt", kwargs={"payment_ref": intent.payment_id})
        self.assertEqual(TenantAPIClient(bola_user).get(url).status_code, 404)
