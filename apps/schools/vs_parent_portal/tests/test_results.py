"""A parent reads a child's results and report card, and a held result stays held.

Brightfield publishes JSS1 A's First Term with Amina's result held for fees.
Mrs. Okon is mother to Chiamaka (released) and Amina (withheld): each child's
answer is their own, and the held card cannot be opened.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_gradebook.tests.card_base import CardFixture
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, StudentGuardian
from vs_rbac.tests.helpers import make_school_admin


class ParentResultsTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])
        cls.okon_user = make_school_admin(
            None, email="okon.parent@example.ng", tenant=cls.tenant,
            first_name="Ify", last_name="Okon",
        )
        okon = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ify", last_name="Okon",
            phone="08110000020", user=cls.okon_user,
        )
        for pupil in (cls.chiamaka, cls.amina):
            StudentGuardian.all_objects.create(
                tenant=cls.tenant, student=pupil, guardian=okon,
                relationship=Relationship.MOTHER, is_primary=False,
            )

    def read(self, name, pupil, **params):
        url = reverse(name, kwargs={"child_id": pupil.pk})
        return TenantAPIClient(self.okon_user).get(url, params)

    def test_a_released_term_comes_back_for_the_child_it_was_asked_for(self):
        response = self.read("parent-portal-results", self.chiamaka)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["state"], "released")
        self.assertEqual(data["term"]["id"], self.first_term.pk)
        self.assertTrue(data["subjects"])

    def test_a_held_result_is_withheld_with_no_figures(self):
        data = self.read("parent-portal-results", self.amina).data["data"]
        self.assertEqual(data["state"], "withheld")
        for hidden in ("subjects", "summary", "teacher_comment"):
            self.assertNotIn(hidden, data)

    def test_the_released_card_opens_and_the_held_card_does_not(self):
        card = self.read("parent-portal-report-card", self.chiamaka, term=self.first_term.pk)
        self.assertEqual(card.status_code, 200, card.data)
        self.assertFalse(card.data["data"]["held"])
        held = self.read("parent-portal-report-card", self.amina, term=self.first_term.pk)
        self.assertEqual(held.status_code, 404)

    def test_the_year_view_answers_for_the_child(self):
        response = self.read("parent-portal-results-session", self.chiamaka)
        self.assertEqual(response.status_code, 200, response.data)
