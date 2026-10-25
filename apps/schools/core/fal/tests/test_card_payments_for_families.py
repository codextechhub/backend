"""Families pay school fees by bank transfer unless the school switches card on.

Corona runs two branches and Greenfield one. At Corona, Tunde (Ikeja) and Bola
(Lekki) each owe a term bill; Tunde's mother has a parent app account and so
has Bola's, Tunde has a student app account, and Greenfield's Ada owes a bill
of her own. Nobody has switched card payments on, which is how every school
starts: the pupil and parent apps refuse to start a card checkout, say so on
their fees payload so the apps hide card, and the emailed pay link starts
nothing either. Corona switching card on changes nothing at Greenfield. The
school's own desk is not touched by any of it.
"""
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.family_payments import CARD_OFF_MESSAGE, NO_ACCOUNT_MESSAGE
from schools.core.fal.student_payments import family_card_payments_enabled
from schools.core.fal.tests.base import FALFixture
from schools.vs_student_portal.fees import (
    TRANSFER_UNAVAILABLE_OR_CARD_SENTENCE,
    TRANSFER_UNAVAILABLE_SENTENCE,
)
from schools.vs_student_portal.tests.test_fees import give_access, set_school_config
from vs_finance.pay_links import make_invoice_pay_token
from vs_payments.constants import CollectionOrigin
from vs_payments.exceptions import ProviderNotConfiguredError
from vs_payments.models import CollectionIntent
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider

CARD = "fees.family_card_payments_enabled"
UNAVAILABLE = "UNAVAILABLE"


class _CardFixture(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        bridge = DjangoFeeTermBridgeAdapter()
        cls.session, cls.term = cls.session_and_term(cls.corona)
        cls.structure = cls.fee_structure(cls.corona_books, amount=300_000)
        bridge.link_term(cls.structure.pk, cls.session.pk, cls.term.pk)
        g_session, g_term = cls.session_and_term(cls.greenfield)
        cls.g_structure = cls.fee_structure(cls.greenfield_books, amount=200_000)
        bridge.link_term(cls.g_structure.pk, g_session.pk, g_term.pk)

        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.bola = cls.student(cls.corona, cls.lekki, first="Bola", last="Okoro")
        cls.ada = cls.student(cls.greenfield, cls.greenfield_main, first="Ada", last="Eze")
        for books, pupil, branch in (
            (cls.corona_books, cls.tunde, cls.ikeja), (cls.corona_books, cls.bola, cls.lekki),
            (cls.greenfield_books, cls.ada, cls.greenfield_main),
        ):
            cls.student_customer(books, str(pupil.pk), branch=branch)
        bridge.generate_cohort_invoices(cls.structure.pk, (str(cls.tunde.pk), str(cls.bola.pk)))
        bridge.generate_cohort_invoices(cls.g_structure.pk, (str(cls.ada.pk),))
        cls.tunde_invoice = Invoice.objects.get(customer__source_id=str(cls.tunde.pk))
        cls.bola_invoice = Invoice.objects.get(customer__source_id=str(cls.bola.pk))
        cls.ada_invoice = Invoice.objects.get(customer__source_id=str(cls.ada.pk))

        cls.tunde_user = give_access(cls.tunde, "COR/001")
        cls.ada_user = give_access(cls.ada, "GRN/001")
        cls.tunde_mum = cls.mother_of(cls.corona, cls.tunde, "tunde.mum@corona.test", "Mrs Adeyemi")
        cls.bola_mum = cls.mother_of(cls.corona, cls.bola, "bola.mum@corona.test", "Mrs Okoro")
        cls.ada_mum = cls.mother_of(cls.greenfield, cls.ada, "ada.mum@greenfield.test", "Mrs Eze")

    @classmethod
    def mother_of(cls, school, pupil, email, name):
        guardian = cls.guardian_of(school, pupil, full_name=name)
        guardian.user = cls.user_for(school, email)
        guardian.save(update_fields=["user"])
        return guardian.user

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)

    def card(self, school, on=True):
        set_school_config(school.tenant, self.bursar, CARD, on)

    def pupil_pays(self, user, invoice, amount=300_000):
        url = reverse("student-portal-pay", kwargs={"invoice_ref": invoice.pk})
        return TenantAPIClient(user).post(url, {"amount": amount}, format="json")

    def parent_pays(self, user, invoice, amount=300_000):
        url = reverse("parent-portal-pay", kwargs={"invoice_ref": invoice.pk})
        return TenantAPIClient(user).post(url, {"amount": amount}, format="json")

    def intents(self):
        return CollectionIntent.objects.count()


class PupilPortalCardTests(_CardFixture):
    def test_a_pupil_cannot_start_a_card_checkout_at_a_school_that_takes_no_card(self):
        response = self.pupil_pays(self.tunde_user, self.tunde_invoice)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "CARD_PAYMENTS_OFF")
        self.assertEqual(self.intents(), 0)

    def test_a_one_branch_school_refuses_too(self):
        response = self.pupil_pays(self.ada_user, self.ada_invoice, 200_000)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "CARD_PAYMENTS_OFF")

    def test_the_fees_payload_says_card_is_off_so_the_app_hides_it(self):
        data = TenantAPIClient(self.tunde_user).get(reverse("student-portal-fees")).data["data"]
        self.assertIs(data["card_payments"], False)
        paged = TenantAPIClient(self.tunde_user).get(
            reverse("student-portal-fees"), {"page": 1},
        ).data["data"]
        self.assertIs(paged["card_payments"], False)

    def test_switching_card_on_restores_the_checkout_and_the_flag(self):
        self.card(self.corona)
        data = TenantAPIClient(self.tunde_user).get(reverse("student-portal-fees")).data["data"]
        self.assertIs(data["card_payments"], True)
        response = self.pupil_pays(self.tunde_user, self.tunde_invoice)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["checkout_url"])

    def test_one_school_switching_card_on_does_not_switch_it_on_for_another(self):
        self.card(self.corona)
        self.assertTrue(family_card_payments_enabled(self.corona.tenant))
        self.assertFalse(family_card_payments_enabled(self.greenfield.tenant))
        response = self.pupil_pays(self.ada_user, self.ada_invoice, 200_000)
        self.assertEqual(response.status_code, 409)

    def test_the_pupil_payment_switch_still_comes_first(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.student_payment_enabled", False)
        response = self.pupil_pays(self.tunde_user, self.tunde_invoice)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "STUDENT_PAYMENT_OFF")

    def test_an_invoice_that_is_not_the_pupils_is_still_a_refusal_not_a_leak(self):
        self.card(self.corona)
        response = self.pupil_pays(self.tunde_user, self.bola_invoice)
        self.assertEqual(response.status_code, 404)


class ParentPortalCardTests(_CardFixture):
    def test_a_parent_cannot_start_a_card_checkout_at_a_school_that_takes_no_card(self):
        response = self.parent_pays(self.tunde_mum, self.tunde_invoice)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "CARD_PAYMENTS_OFF")
        self.assertEqual(self.intents(), 0)

    def test_a_one_branch_school_refuses_too(self):
        response = self.parent_pays(self.ada_mum, self.ada_invoice, 200_000)
        self.assertEqual(response.status_code, 409)

    def test_another_familys_invoice_is_a_404_not_a_card_refusal(self):
        response = self.parent_pays(self.tunde_mum, self.bola_invoice)
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_fees_payload_says_card_is_off(self):
        data = TenantAPIClient(self.tunde_mum).get(reverse("parent-portal-fees")).data["data"]
        self.assertIs(data["card_payments"], False)
        self.assertEqual(data["pay_all"]["steps"][0]["invoice_ref"], self.tunde_invoice.pk)

    def test_switching_card_on_restores_the_checkout_and_the_flag(self):
        self.card(self.corona)
        data = TenantAPIClient(self.tunde_mum).get(reverse("parent-portal-fees")).data["data"]
        self.assertIs(data["card_payments"], True)
        response = self.parent_pays(self.tunde_mum, self.tunde_invoice)
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_pupil_payment_switch_never_stops_a_parent_from_the_card_when_it_is_on(self):
        self.card(self.corona)
        set_school_config(self.corona.tenant, self.bursar, "fees.student_payment_enabled", False)
        self.assertEqual(self.parent_pays(self.tunde_mum, self.tunde_invoice).status_code, 201)

    def test_one_school_s_choice_does_not_reach_another_school_s_parent(self):
        self.card(self.corona)
        self.assertEqual(self.parent_pays(self.ada_mum, self.ada_invoice, 200_000).status_code, 409)


class MissingAccountSentenceTests(_CardFixture):
    """A family with no account number yet is told it is being set up."""

    def ask_pupil(self, user=None):
        with mock.patch.object(
            self.fake, "create_virtual_account", side_effect=ProviderNotConfiguredError("no"),
        ):
            return TenantAPIClient(user or self.tunde_user).post(
                reverse("student-portal-transfer-account"), format="json",
            ).data["data"]

    def ask_parent(self, mum, pupil):
        with mock.patch.object(
            self.fake, "create_virtual_account", side_effect=ProviderNotConfiguredError("no"),
        ):
            return TenantAPIClient(mum).post(
                reverse("parent-portal-transfer-account", kwargs={"child_id": pupil.pk}),
                format="json",
            ).data["data"]

    def test_the_sentence_has_no_card_in_it_where_card_is_off(self):
        for data in (self.ask_pupil(), self.ask_parent(self.tunde_mum, self.tunde)):
            self.assertEqual(data["state"], UNAVAILABLE)
            self.assertEqual(
                data["message"],
                "Your payment account is being set up. Try again later, or contact the school office.",
            )
            self.assertNotIn("card", data["message"].lower())

    def test_the_sentence_may_mention_card_where_card_is_on(self):
        self.card(self.corona)
        for data in (self.ask_pupil(), self.ask_parent(self.tunde_mum, self.tunde)):
            self.assertEqual(data["message"], TRANSFER_UNAVAILABLE_OR_CARD_SENTENCE)
        self.assertNotEqual(TRANSFER_UNAVAILABLE_OR_CARD_SENTENCE, TRANSFER_UNAVAILABLE_SENTENCE)

    def test_a_one_branch_school_reads_the_same_sentence(self):
        data = self.ask_parent(self.ada_mum, self.ada)
        self.assertEqual(data["message"], TRANSFER_UNAVAILABLE_SENTENCE)

    def test_another_family_s_child_is_still_a_404(self):
        response = TenantAPIClient(self.tunde_mum).post(
            reverse("parent-portal-transfer-account", kwargs={"child_id": self.bola.pk}),
            format="json",
        )
        self.assertEqual(response.status_code, 404)


@override_settings(
    SCHOOL_APP_BASE_URL="https://xvs.codexng.com", PAYMENTS_CALLBACK_URL="",
    PLATFORM_PAY_BASE_URL="",
)
class PublicPayLinkTests(_CardFixture):
    """The link in a family's invoice email starts a checkout only where card is on."""

    def link(self, invoice, name):
        return reverse(name, args=[make_invoice_pay_token(invoice)])

    def test_the_link_refuses_a_checkout_at_a_school_that_takes_no_card(self):
        response = APIClient().post(self.link(self.tunde_invoice, "public-invoice-checkout"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CARD_PAYMENTS_OFF")
        self.assertEqual(response.json()["message"], CARD_OFF_MESSAGE)
        self.assertEqual(self.intents(), 0)

    def test_the_page_still_shows_the_balance(self):
        data = APIClient().get(self.link(self.tunde_invoice, "public-invoice-pay")).json()["data"]
        self.assertEqual(data["amount_due_kobo"], 300_000)
        self.assertFalse(data["payable"])
        self.assertFalse(data["card_payments"])

    def test_the_link_starts_a_checkout_where_card_is_on(self):
        self.card(self.corona)
        response = APIClient().post(self.link(self.tunde_invoice, "public-invoice-checkout"))
        self.assertEqual(response.status_code, 200, response.content)
        data = APIClient().get(self.link(self.tunde_invoice, "public-invoice-pay")).json()["data"]
        self.assertTrue(data["payable"])
        self.assertTrue(data["card_payments"])

    def test_a_one_branch_school_s_link_is_refused_while_the_other_school_s_works(self):
        self.card(self.corona)
        refused = APIClient().post(self.link(self.ada_invoice, "public-invoice-checkout"))
        worked = APIClient().post(self.link(self.tunde_invoice, "public-invoice-checkout"))
        self.assertEqual((refused.status_code, worked.status_code), (409, 200))

    def test_a_customer_who_is_not_a_pupil_is_unaffected(self):
        from vs_finance.models import Customer, Invoice

        hall = Customer.objects.create(
            entity_id=self.corona_books.entity_ref, code="HALL-1", name="Hall hire",
            receivable_account=self.tunde_invoice.customer.receivable_account,
            branch=self.ikeja,
        )
        Invoice.objects.filter(pk=self.tunde_invoice.pk).update(customer=hall)
        response = APIClient().post(self.link(self.tunde_invoice, "public-invoice-checkout"))
        self.assertEqual(response.status_code, 200, response.content)


class StaffCollectionsAreUntouchedTests(_CardFixture):
    """The school's own desk starts card collections whatever the families' switch says."""

    def test_a_desk_collection_still_starts_with_card_off(self):
        from vs_payments.services import initiate_collection

        intent = initiate_collection(
            entity=self.tunde_invoice.entity, amount=100_000,
            customer=self.tunde_invoice.customer, invoice=self.tunde_invoice,
            payer_email="front.desk@corona.test", payer_name="Front desk",
            actor_user=self.bursar, origin=CollectionOrigin.DESK,
        )
        self.assertTrue(intent.checkout_url)
        self.assertEqual(intent.origin, CollectionOrigin.DESK)

    def test_the_collections_endpoint_still_works_for_staff(self):
        self.grant(self.bursar, "payments.collection.create")
        response = TenantAPIClient(self.bursar).post(
            f"{reverse('payments-collections')}?entity={self.corona_books.entity_ref}",
            {
                "amount": 100_000, "customer": self.tunde_invoice.customer_id,
                "invoice": self.tunde_invoice.pk, "origin": "DESK",
                "payer_email": "front.desk@corona.test",
            },
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), getattr(response, "data", None))
        self.assertEqual(self.intents(), 1)
