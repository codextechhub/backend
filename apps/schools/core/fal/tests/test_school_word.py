"""The fee labels say the school's word for a term.

Corona says Term, as its three-term structure implies. Greenfield runs two
semesters, so its bursar reads "This semester" on the dashboard switch and "End
of the semester billed" among the due rules. A term's own name is printed as
the school stored it either way.
"""
from __future__ import annotations

import datetime

from django.urls import reverse
from rest_framework.test import APIClient

from vs_user.tokens import CodeXRefreshToken

from .base import FALFixture


class FeeLabelWordTests(FALFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_schools.models import School

        School.objects.filter(pk=cls.greenfield.pk).update(term_structure="2_SEMESTERS")

    def client_for(self, user, *keys):
        for key in keys:
            self.grant(user, key)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def this_period(self, school, books):
        from vs_finance.models import LedgerEntity

        from ..billing_periods import current_term

        session, _term = self.session_and_term(school)
        session.status = "ACTIVE"
        session.save(update_fields=["status"])
        return current_term(
            LedgerEntity.objects.get(pk=books.entity_ref), datetime.date(2026, 10, 1),
        )

    def test_a_term_school_reads_this_term(self):
        period = self.this_period(self.corona, self.corona_books)
        self.assertEqual(period.label, "This term")

    def test_a_semester_school_reads_this_semester_and_its_own_term_name(self):
        period = self.this_period(self.greenfield, self.greenfield_books)
        self.assertEqual(period.label, "This semester")
        self.assertIn("First Term", period.name)

    def test_the_due_rules_name_the_semester_billed(self):
        response = self.client_for(self.greenfield_bursar, "school.fees.view").get(
            f"{reverse('fal-fee-due-policy')}?tenant={self.greenfield.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data["data"]
        self.assertEqual(data["basis_display"], "End of the semester billed")
        labels = {o["value"]: o["label"] for o in data["options"]}
        self.assertEqual(labels["TERM_END"], "End of the semester billed")
        self.assertEqual(labels["SESSION_END"], "End of the session billed")

    def test_the_due_rules_name_the_term_billed_at_a_term_school(self):
        response = self.client_for(self.bursar, "school.fees.view").get(
            f"{reverse('fal-fee-due-policy')}?tenant={self.corona.tenant.slug}",
        )
        self.assertEqual(response.data["data"]["basis_display"], "End of the term billed")

    def test_an_unlinked_structure_is_described_in_the_schools_word(self):
        structure = self.fee_structure(self.greenfield_books)
        response = self.client_for(
            self.greenfield_bursar, "finance.feestructure.view",
        ).get(
            reverse("fal-fee-structure-link-term", kwargs={"pk": structure.pk}),
            {"tenant": self.greenfield.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.data["message"], "Fee structure is not linked to a semester.",
        )
