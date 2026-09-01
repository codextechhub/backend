"""The FAL's HTTP surface.

Security first, because this route creates money owed by real families: a caller
without the key is refused, and one school cannot touch another's fee structure
by guessing an id.
"""
from __future__ import annotations

from rest_framework.test import APIClient

from vs_user.tokens import CodeXRefreshToken

from .base import FALFixture


class FalRouteTests(FALFixture):
    def setUp(self):
        super().setUp()
        # Default: no credentials, Corona asserted. as_bursar() replaces both.
        self.client = APIClient()
        self.slug = self.corona.tenant.slug
        self.books = self.corona_books
        self.structure = self.fee_structure(self.books, code="JSS1-TUITION")
        self.session, self.term = self.session_and_term(self.corona)

    # ---- helpers ---------------------------------------------------------
    # The real auth path, not force_authenticate: request.tenant is set by the
    # authentication class from the mandatory ?tenant= assertion, so a test that
    # skipped it would never exercise the scoping this endpoint depends on.
    def link_url(self, pk=None):
        return f"/v1/school-finance/fee-structures/{pk or self.structure.pk}/link-term/"

    def gen_url(self, pk=None):
        return f"/v1/school-finance/fee-structures/{pk or self.structure.pk}/generate-invoices/"

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def as_bursar(self, *keys, user=None):
        user = user or self.bursar
        for key in keys:
            self.grant(user, key)
        self.client = self.client_for(user)
        self.slug = user.tenant.slug
        return user

    def post(self, url, body=None):
        return self.client.post(
            f"{url}?tenant={self.slug}", body or {}, format="json",
        )

    # ---- security --------------------------------------------------------
    def test_an_anonymous_caller_is_refused(self):
        self.assertEqual(self.post(self.link_url(), {}).status_code, 401)

    def test_a_signed_in_caller_without_the_key_is_refused(self):
        self.client = self.client_for(self.bursar)
        self.slug = self.corona.tenant.slug
        res = self.post(self.link_url(), {"session": self.session.pk})
        self.assertEqual(res.status_code, 403)

    def test_linking_needs_edit_and_billing_needs_generate(self):
        """The two verbs are separate keys, so reading is not billing."""
        self.as_bursar("finance.feestructure.edit")
        self.assertEqual(
            self.post(self.gen_url(), {"students": ["1"]}).status_code,
            403,
        )

    def test_another_school_cannot_reach_this_structure_and_gets_404(self):
        """404 not 403: a 403 would confirm the structure exists."""
        self.as_bursar("finance.feestructure.edit", user=self.greenfield_bursar)
        res = self.post(self.link_url(), {"session": self.session.pk})
        self.assertEqual(res.status_code, 404)

    # ---- linking ---------------------------------------------------------
    def test_a_structure_can_be_linked_to_a_term(self):
        self.as_bursar("finance.feestructure.edit")
        res = self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["fee_structure"], self.structure.pk)
        self.assertEqual(res.data["data"]["term"], self.term.pk)

    # ---- billing ---------------------------------------------------------
    def test_billing_an_unlinked_structure_is_refused_with_a_reason(self):
        """A structure prices one term and cannot bill before it names one."""
        self.as_bursar("finance.feestructure.generate")
        student = self.student(self.corona, self.ikeja)
        res = self.post(self.gen_url(), {"students": [str(student.pk)]})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data.get("code"), "TERM_NOT_LINKED")

    def test_a_dry_run_bills_nobody_and_says_so(self):
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        student = self.student(self.corona, self.ikeja)

        res = self.post(self.gen_url(), {"students": [str(student.pk)], "dry_run": True})
        self.assertEqual(res.status_code, 200, res.data)
        body = res.data["data"]
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["invoices_created"], [])
        self.assertEqual(body["counts"]["to_bill"], 1)
        # and nothing was actually billed
        after = self.post(self.gen_url(), {"students": [str(student.pk)], "dry_run": True})
        self.assertEqual(after.data["data"]["counts"]["skipped"], 0)

    def test_a_real_run_bills_once_and_the_second_run_skips(self):
        """Re-running is correct behaviour, not an error."""
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        student = self.student(self.corona, self.ikeja)
        body = {"students": [str(student.pk)]}

        first = self.post(self.gen_url(), body)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["data"]["counts"]["created"], 1)

        second = self.post(self.gen_url(), body)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["data"]["counts"]["created"], 0)
        self.assertEqual(second.data["data"]["counts"]["skipped"], 1)

    def test_an_empty_cohort_is_refused_rather_than_billing_everyone(self):
        """The neutral engine's batch bills every active customer. This must not."""
        self.as_bursar("finance.feestructure.generate")
        res = self.post(self.gen_url(), {"students": []})
        self.assertEqual(res.status_code, 400)

    def test_a_child_named_twice_is_billed_once(self):
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        student = self.student(self.corona, self.ikeja)
        res = self.post(self.gen_url(), {"students": [str(student.pk), str(student.pk)]})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["data"]["counts"]["created"], 1)
