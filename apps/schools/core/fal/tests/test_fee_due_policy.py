"""When a school's fee bills fall due.

The engine's answer is the entity's payment terms, which is a trade-credit rule:
right for a supplier invoice, wrong for school fees. A term's fees are due by a
date in the school's calendar, and these pin that the school's rule is what
reaches the invoice.
"""
from __future__ import annotations

import datetime

from django.urls import reverse
from rest_framework.test import APIClient

from vs_user.tokens import CodeXRefreshToken

from .base import FALFixture


class DueDateResolutionTests(FALFixture):
    """The rule on its own, before any HTTP or billing is involved."""

    def _resolve(self, basis, **kwargs):
        from ..due_dates import resolve_due_date

        return resolve_due_date(
            basis=basis, days_after=kwargs.pop("days_after", 30),
            invoice_date=kwargs.pop("invoice_date", datetime.date(2026, 9, 4)),
            **kwargs,
        )

    def test_term_end_bills_to_the_end_of_the_term(self):
        self.assertEqual(
            self._resolve(
                "TERM_END", term_end=datetime.date(2026, 11, 15),
                session_end=datetime.date(2027, 7, 16),
            ),
            datetime.date(2026, 11, 15),
        )

    def test_term_end_falls_back_to_the_session_for_a_whole_year_fee(self):
        """A structure linked to a session and no term still has an answer."""
        self.assertEqual(
            self._resolve(
                "TERM_END", term_end=None, session_end=datetime.date(2027, 7, 16),
            ),
            datetime.date(2027, 7, 16),
        )

    def test_month_end_bills_to_the_end_of_the_billing_month(self):
        self.assertEqual(
            self._resolve("MONTH_END"), datetime.date(2026, 9, 30),
        )

    def test_month_end_handles_february_in_a_leap_year(self):
        self.assertEqual(
            self._resolve("MONTH_END", invoice_date=datetime.date(2028, 2, 3)),
            datetime.date(2028, 2, 29),
        )

    def test_days_after_bills_that_many_days_on(self):
        self.assertEqual(
            self._resolve("DAYS_AFTER", days_after=21), datetime.date(2026, 9, 25),
        )

    def test_a_rule_with_no_date_still_produces_a_deadline(self):
        """An unresolvable rule must not fall through to null.

        A school with no academic calendar rows yet still bills, and a null due
        date is not "no deadline" to any report that reads it - it is never
        overdue.
        """
        self.assertEqual(
            self._resolve("SESSION_END", session_end=None, days_after=30),
            datetime.date(2026, 10, 4),
        )

    def test_a_late_run_is_payable_now_rather_than_already_late(self):
        """The floor.

        A bursar catching up bills First Term on 20 November, five days after
        the term ended. Without the floor those parents receive invoices that
        were overdue before they existed, and the school duns them for its own
        lateness.
        """
        self.assertEqual(
            self._resolve(
                "TERM_END", invoice_date=datetime.date(2026, 11, 20),
                term_end=datetime.date(2026, 11, 15),
            ),
            datetime.date(2026, 11, 20),
        )


class FeeDuePolicyEndpointTests(FALFixture):
    """The settings screen behind it."""

    def setUp(self):
        super().setUp()
        self.url = reverse("fal-fee-due-policy")

    def _client(self, user, keys=("school.fees.view", "school.fees.manage")):
        """The real auth path, not force_authenticate.

        ``request.tenant`` is set by the authentication class from the mandatory
        ``?tenant=`` assertion, so a test that skipped it would never exercise
        the scoping this endpoint depends on.
        """
        for key in keys:
            self.grant(user, key)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def test_a_school_that_has_never_set_one_reads_the_default_it_bills_by(self):
        """No unset state: billing always picks a date, so the screen says which."""
        response = self._client(self.bursar).get(
            f"{self.url}?tenant={self.corona.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["basis"], "TERM_END")

    def test_every_option_shows_the_date_it_would_produce(self):
        """A rule name is an abstraction; a date is a decision."""
        response = self._client(self.bursar).get(
            f"{self.url}?tenant={self.corona.tenant.slug}",
        )
        options = response.data["data"]["options"]
        self.assertEqual(len(options), 4)
        for option in options:
            with self.subTest(option=option["value"]):
                self.assertTrue(option["due_if_billed_today"])

    def test_the_school_can_change_the_rule(self):
        response = self._client(self.bursar).patch(
            f"{self.url}?tenant={self.corona.tenant.slug}",
            {"basis": "MONTH_END"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["data"]["basis"], "MONTH_END")

    def test_a_year_of_credit_on_a_terms_fees_is_refused(self):
        response = self._client(self.bursar).patch(
            f"{self.url}?tenant={self.corona.tenant.slug}",
            {"basis": "DAYS_AFTER", "days_after": 400}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_reading_it_does_not_let_you_change_it(self):
        response = self._client(self.bursar, keys=("school.fees.view",)).patch(
            f"{self.url}?tenant={self.corona.tenant.slug}",
            {"basis": "MONTH_END"}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_another_school_cannot_read_this_ones_rule(self):
        response = self._client(self.greenfield_bursar).get(
            f"{self.url}?tenant={self.corona.tenant.slug}",
        )
        self.assertIn(response.status_code, (403, 404))
