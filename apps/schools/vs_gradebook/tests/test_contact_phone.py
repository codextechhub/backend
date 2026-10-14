"""The phone a family is given on a report card and in the emails about it.

A family calls the branch that holds the card: the class's branch phone, the
school's own when the branch has none, and no phone line at all when neither
has one.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from schools.vs_gradebook.services import publishing, reportcards, resolve
from schools.vs_schools.models import School
from vs_tenants.models import Branch, Tenant

from .card_base import SEND, CardFixture

SCHOOL_PHONE = "08012345678"
LEKKI_PHONE = "0801 234 5678"
IKEJA_PHONE = "0802 999 0000"


class ContactPhoneTests(SimpleTestCase):
    @staticmethod
    def tenant(phone):
        return SimpleNamespace(school_profile=SimpleNamespace(phone=phone))

    def test_the_branch_phone_wins(self):
        branch = SimpleNamespace(phone=LEKKI_PHONE)
        self.assertEqual(resolve.contact_phone(self.tenant(SCHOOL_PHONE), branch), LEKKI_PHONE)

    def test_a_branch_without_one_falls_back_to_the_schools(self):
        for blank in ("", "  ", None):
            with self.subTest(blank=blank):
                self.assertEqual(
                    resolve.contact_phone(self.tenant(SCHOOL_PHONE), SimpleNamespace(phone=blank)),
                    SCHOOL_PHONE,
                )

    def test_neither_gives_an_empty_string(self):
        self.assertEqual(resolve.contact_phone(self.tenant(""), SimpleNamespace(phone="")), "")
        self.assertEqual(resolve.contact_phone(SimpleNamespace(), None), "")


class PhoneFixture(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        School.objects.filter(tenant=cls.tenant).update(phone=SCHOOL_PHONE)
        School.objects.filter(tenant=cls.solo).update(phone="08055550000")
        cls.class_term = cls.publish_jss1a()

    @property
    def fresh_tenant(self):
        """The tenant as a request loads it, so the school's phone is read from the row."""
        return Tenant.objects.get(pk=self.tenant.pk)

    def set_branch_phone(self, branch, phone):
        Branch.all_objects.filter(pk=branch.pk).update(phone=phone)

    def letterhead_phone(self):
        return self.data(self.report_card(self.releaser, self.tunde))["letterhead"]["phone"]


class CardPhoneTests(PhoneFixture):
    def test_the_card_prints_the_class_branch_phone(self):
        self.set_branch_phone(self.lekki, LEKKI_PHONE)
        self.assertEqual(self.letterhead_phone(), LEKKI_PHONE)

    def test_the_printed_class_carries_the_same_phone_on_every_card(self):
        self.set_branch_phone(self.lekki, LEKKI_PHONE)
        cards = self.data(self.class_cards(self.releaser))["cards"]
        self.assertTrue(cards)
        self.assertEqual({card["letterhead"]["phone"] for card in cards}, {LEKKI_PHONE})

    def test_a_branch_without_a_phone_prints_the_schools(self):
        self.assertEqual(self.letterhead_phone(), SCHOOL_PHONE)

    def test_neither_phone_leaves_the_line_off(self):
        School.objects.filter(tenant=self.tenant).update(phone="")
        self.assertIsNone(self.letterhead_phone())

    def test_two_branches_each_print_their_own_phone(self):
        self.set_branch_phone(self.lekki, LEKKI_PHONE)
        self.set_branch_phone(self.ikeja, IKEJA_PHONE)
        lekki = reportcards.letterhead(self.fresh_tenant, Branch.all_objects.get(pk=self.lekki.pk))
        ikeja = reportcards.letterhead(self.fresh_tenant, Branch.all_objects.get(pk=self.ikeja.pk))
        self.assertEqual((lekki["phone"], ikeja["phone"]), (LEKKI_PHONE, IKEJA_PHONE))
        self.assertEqual(self.letterhead_phone(), LEKKI_PHONE)

    def test_the_card_of_a_one_branch_school_prints_its_branch_phone(self):
        self.publish_sunrise()
        Branch.all_objects.filter(pk=self.solo_branch.pk).update(phone="0803 111 2222")
        response = self.call(
            "get", self.solo_releaser, "gradebook-report-card", class_id=self.solo_class.pk,
            student_id=self.solo_pupil.pk, term=self.solo_term,
        )
        self.assertEqual(self.data(response)["letterhead"]["phone"], "0803 111 2222")


class EmailPhoneTests(PhoneFixture):
    def contexts(self, send):
        return [call.kwargs["context"] for call in send.call_args_list]

    def ready(self):
        with mock.patch(SEND, return_value=["sent"]) as send:
            publishing.send_ready_notices(
                self.fresh_tenant, self.class_term.pk, [self.chiamaka.pk],
            )
        return self.contexts(send)

    def updated(self):
        with mock.patch(SEND, return_value=["sent"]) as send:
            publishing.send_updated_notice(self.fresh_tenant, self.class_term.pk, self.chiamaka.pk)
        return self.contexts(send)

    def test_both_emails_carry_the_class_branch_phone(self):
        self.set_branch_phone(self.lekki, LEKKI_PHONE)
        for sent in (self.ready(), self.updated()):
            with self.subTest():
                [context] = sent
                self.assertEqual(context["tenant_phone"], LEKKI_PHONE)

    def test_both_emails_fall_back_to_the_schools_phone(self):
        for sent in (self.ready(), self.updated()):
            with self.subTest():
                [context] = sent
                self.assertEqual(context["tenant_phone"], SCHOOL_PHONE)

    def test_neither_phone_gives_an_empty_value_the_template_leaves_off(self):
        School.objects.filter(tenant=self.tenant).update(phone="")
        [context] = self.ready()
        self.assertEqual(context["tenant_phone"], "")

    def test_two_branches_give_each_class_its_own_phone(self):
        self.set_branch_phone(self.lekki, LEKKI_PHONE)
        self.set_branch_phone(self.ikeja, IKEJA_PHONE)
        lekki = Branch.all_objects.get(pk=self.lekki.pk)
        ikeja = Branch.all_objects.get(pk=self.ikeja.pk)
        term, student = self.class_term.term, self.chiamaka
        self.assertEqual(
            publishing.ready_context(self.fresh_tenant, lekki, term, student)["tenant_phone"], LEKKI_PHONE,
        )
        self.assertEqual(
            publishing.ready_context(self.fresh_tenant, ikeja, term, student)["tenant_phone"], IKEJA_PHONE,
        )

    def test_a_one_branch_school_uses_its_branch_phone(self):
        Branch.all_objects.filter(pk=self.solo_branch.pk).update(phone="0803 111 2222")
        branch = Branch.all_objects.get(pk=self.solo_branch.pk)
        context = publishing.ready_context(self.solo, branch, self.class_term.term, self.chiamaka)
        self.assertEqual(context["tenant_phone"], "0803 111 2222")
