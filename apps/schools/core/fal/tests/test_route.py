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

    # ---- branch scope of the cohort ---------------------------------------
    def as_lekki_bursar(self):
        """A bursar whose grants are pinned to Lekki, linking and billing there."""
        for key in ("finance.feestructure.edit", "finance.feestructure.generate"):
            self.grant(self.lekki_bursar, key, branch=self.lekki)
        self.client = self.client_for(self.lekki_bursar)
        self.slug = self.lekki_bursar.tenant.slug

    def test_a_branch_bursar_cannot_bill_a_child_at_another_branch(self):
        """The structure is shared, so she reaches it; the Ikeja child is not hers.

        404 rather than 403, as for the structure: a refusal must not confirm
        that the id names a child at all. Nothing is billed, not even the Lekki
        child named beside them, because a cohort is billed whole or not at all.
        """
        from vs_finance.models import Invoice

        self.as_lekki_bursar()
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        ikeja_child = self.student(self.corona, self.ikeja)
        lekki_child = self.student(self.corona, self.lekki, first="Amaka")

        res = self.post(
            self.gen_url(), {"students": [str(lekki_child.pk), str(ikeja_child.pk)]},
        )
        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(Invoice.objects.filter(entity_id=self.books.entity_ref).exists())

    def test_a_branch_bursar_previews_and_bills_her_own_branch(self):
        self.as_lekki_bursar()
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        lekki_child = self.student(self.corona, self.lekki, first="Amaka")
        body = {"students": [str(lekki_child.pk)]}

        preview = self.post(self.gen_url(), {**body, "dry_run": True})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(preview.data["data"]["counts"]["to_bill"], 1)

        run = self.post(self.gen_url(), body)
        self.assertEqual(run.status_code, 201, run.data)
        self.assertEqual(run.data["data"]["counts"]["created"], 1)

    def test_a_school_wide_bursar_bills_children_at_every_branch(self):
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        ikeja_child = self.student(self.corona, self.ikeja)
        lekki_child = self.student(self.corona, self.lekki, first="Amaka")

        res = self.post(
            self.gen_url(), {"students": [str(ikeja_child.pk), str(lekki_child.pk)]},
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["data"]["counts"]["created"], 2)

    def test_a_branch_price_list_refuses_a_child_from_another_branch(self):
        """Ikeja's JSS 1 fee is not Lekki's, even to a bursar who sees both."""
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        ikeja_fees = self.fee_structure(self.books, code="IKJ-JSS1", branch=self.ikeja)
        self.post(self.link_url(ikeja_fees.pk), {"session": self.session.pk, "term": self.term.pk})
        ikeja_child = self.student(self.corona, self.ikeja)
        lekki_child = self.student(self.corona, self.lekki, first="Amaka")

        for dry_run in (True, False):
            res = self.post(
                self.gen_url(ikeja_fees.pk),
                {"students": [str(ikeja_child.pk), str(lekki_child.pk)], "dry_run": dry_run},
            )
            self.assertEqual(res.status_code, 409, res.data)
            self.assertEqual(res.data.get("code"), "WRONG_BRANCH")
        # nothing was billed: the Ikeja child alone still bills once
        ok = self.post(self.gen_url(ikeja_fees.pk), {"students": [str(ikeja_child.pk)]})
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(ok.data["data"]["counts"]["created"], 1)

    def test_a_school_wide_price_list_bills_every_branch(self):
        """No branch on the structure means it prices the whole school."""
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        lekki_child = self.student(self.corona, self.lekki, first="Amaka")
        res = self.post(self.gen_url(), {"students": [str(lekki_child.pk)], "dry_run": True})
        self.assertEqual(res.status_code, 200, res.data)

    # ---- reading the link -------------------------------------------------
    def get(self, url):
        return self.client.get(f"{url}?tenant={self.slug}")

    def test_reading_the_link_needs_the_view_key_only(self):
        self.as_bursar("finance.feestructure.view")
        self.assertEqual(self.get(self.link_url()).status_code, 200)
        self.assertEqual(
            self.post(self.link_url(), {"session": self.session.pk}).status_code, 403,
        )

    def test_reading_the_link_without_the_key_is_refused(self):
        self.client = self.client_for(self.bursar)
        self.assertEqual(self.get(self.link_url()).status_code, 403)

    def test_an_unlinked_structure_says_so(self):
        self.as_bursar("finance.feestructure.view")
        res = self.get(self.link_url())
        self.assertEqual(res.data["data"], {"linked": False})

    def test_a_linked_structure_names_its_term(self):
        self.as_bursar("finance.feestructure.view", "finance.feestructure.edit")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        body = self.get(self.link_url()).data["data"]
        self.assertTrue(body["linked"])
        self.assertEqual(body["session"], self.session.pk)
        self.assertEqual(body["term"], self.term.pk)
        self.assertEqual(body["session_label"], self.session.name)
        self.assertEqual(body["term_label"], self.term.name)

    def test_another_school_reading_the_link_gets_404(self):
        self.as_bursar("finance.feestructure.view", user=self.greenfield_bursar)
        self.assertEqual(self.get(self.link_url()).status_code, 404)

    def test_a_branch_bursar_cannot_read_another_branchs_structure(self):
        ikeja_only = self.fee_structure(self.books, code="IKEJA-ONLY", branch=self.ikeja)
        self.grant(self.lekki_bursar, "finance.feestructure.view", branch=self.lekki)
        self.client = self.client_for(self.lekki_bursar)
        self.assertEqual(self.get(self.link_url(ikeja_only.pk)).status_code, 404)

    # ---- the due date on the preview --------------------------------------
    def test_the_preview_carries_the_due_date_the_run_will_write(self):
        """Resolved against the structure's own term, by the rule the run uses.

        The school bills by term end unless it says otherwise, so a structure
        linked to First Term is due when First Term ends, whatever term is
        running on the day the bursar previews it.
        """
        import datetime

        from vs_finance.models import Invoice

        from ..due_dates import resolve_due_date

        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.post(self.link_url(), {"session": self.session.pk, "term": self.term.pk})
        student = self.student(self.corona, self.ikeja)
        expected = resolve_due_date(
            basis="TERM_END", days_after=30, invoice_date=datetime.date.today(),
            term_end=self.term.end_date, session_end=self.session.end_date,
        )

        preview = self.post(self.gen_url(), {"students": [str(student.pk)], "dry_run": True})
        self.assertEqual(preview.data["data"]["due_date"], expected.isoformat())

        run = self.post(self.gen_url(), {"students": [str(student.pk)]})
        self.assertEqual(run.data["data"]["due_date"], expected.isoformat())
        invoice = Invoice.objects.get(pk=run.data["data"]["invoices_created"][0])
        self.assertEqual(invoice.due_date, expected)
