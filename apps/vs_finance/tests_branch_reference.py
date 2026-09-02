"""Reaching one finance row by the reference a caller typed.

``tests_branch_scope`` proves the lists narrow and ``tests_branch_write`` proves
the right branch is stamped on what gets created. Between the two sat the gap
these tests close: **addressing**.

A list is narrowed by ``branch_q``, in thirty-one places in ``views_ar``. The
four resolvers those same screens share when a caller names a row - by customer
code, invoice number, or fee structure code - were narrowed in none of them, so
the row a bursar could not see in her list was one guessed code away from being
read, edited, and billed from. The list hid it; the id reached it.

Two rules are asserted throughout, and they are the reason the fix is a filter
rather than a permission check:

* **another branch's row answers 404, not 403.** A 403 confirms the row exists,
  which turns a customer code into an oracle for which families another branch
  bills. The same answer an unknown code gets is the only safe one, and it is
  what ``get_student_or_404`` and procurement's ``_document_or_404`` already do;
* **a shared row stays reachable by everybody.** A null branch in finance means
  *published for the whole school*, not *belongs to nobody*. A fee template
  Corona publishes once for all three branches must still resolve for the Ikeja
  bursar, or the fix would read as missing data rather than as isolation.

Both shapes of school, because a single-branch test proves nothing about a
multi-branch one - and in the single-branch school the narrowing must be
invisible, since there is no second branch for anything to be hidden from.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.models import Customer, FeeStructure

from .tests_branch_scope import _FinanceBranchFixture


class _ReferenceFixture(_FinanceBranchFixture):
    """Two families and two fee templates per school, one branch-owned, one shared."""

    KEYS = (
        "finance.customer.view", "finance.customer.update",
        "finance.feestructure.view", "finance.feestructure.edit",
        "finance.feestructure.create", "finance.feestructure.generate",
        "finance.report.view",
    )

    def setUp(self):
        super().setUp()

        # Corona, three branches.
        self.ikeja_family = self.customer(self.books, "REFI", self.ikeja)
        self.lekki_family = self.customer(self.books, "REFL", self.lekki)
        self.shared_family = self.customer(self.books, "REFS", None)
        self.lekki_fees = self.fee_structure(self.books, "REFLEK", self.lekki)
        self.shared_fees = self.fee_structure(self.books, "REFALL", None)

        self.bursar = self.caller(
            self.tenant, "ikeja.bursar@example.com", "ref-ikeja", branch=self.ikeja,
        )
        self.head = self.caller(self.tenant, "head@example.com", "ref-head")

        # The single-branch school, where none of this may show.
        self.solo_family = self.customer(self.solo_books, "SOLOF", self.solo_main)
        self.solo_shared_family = self.customer(self.solo_books, "SOLOS", None)
        self.solo_bursar = self.caller(
            self.solo_tenant, "solo.bursar@example.com", "ref-solo",
            branch=self.solo_main,
        )

    def caller(self, tenant, email, role_key, *, branch=None):
        user = self.user_for(tenant, email)
        self.grant(user, *self.KEYS, tenant=tenant, role_key=role_key, branch=branch)
        client = TenantAPIClient(user=user)
        # The client does not expose the user it authenticates as, and one test
        # needs the user itself to ask the scoping layer a question directly.
        client.acting_user = user
        return client

    # -- requests -------------------------------------------------------------- #

    def get(self, client, path, entity, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        return client.get(f"/v1/finance/{path}?entity={entity.code}{query}")

    def patch(self, client, path, entity, body):
        return client.patch(
            f"/v1/finance/{path}?entity={entity.code}", body, format="json",
        )

    def post(self, client, path, entity, body):
        return client.post(
            f"/v1/finance/{path}?entity={entity.code}", body, format="json",
        )


class CustomerByReferenceTests(_ReferenceFixture):
    """A customer code names a family. It must not name another branch's family."""

    def test_a_pinned_bursar_cannot_read_another_branchs_family(self):
        response = self.get(
            self.bursar, f"customers/{self.lekki_family.code}/", self.books,
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_the_refusal_is_indistinguishable_from_an_unknown_code(self):
        """The point of the 404: a code must not report which families exist.

        Both answers are compared in full, not merely by status, because a
        message that named the branch or the family would leak exactly what the
        status code is being careful about.
        """
        real = self.get(
            self.bursar, f"customers/{self.lekki_family.code}/", self.books,
        )
        invented = self.get(self.bursar, "customers/NOSUCHCODE/", self.books)

        self.assertEqual(real.status_code, invented.status_code)
        self.assertEqual(
            str(real.data["message"]).replace(self.lekki_family.code, "X"),
            str(invented.data["message"]).replace("NOSUCHCODE", "X"),
        )

    def test_a_pinned_bursar_cannot_edit_another_branchs_family(self):
        response = self.patch(
            self.bursar, f"customers/{self.lekki_family.code}/", self.books,
            {"name": "Renamed By Ikeja"},
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.lekki_family.refresh_from_db()
        self.assertNotEqual(self.lekki_family.name, "Renamed By Ikeja")

    def test_a_pinned_bursar_cannot_pull_another_branchs_statement_of_account(self):
        """The statement is the whole ledger history of one family.

        It reads through a query parameter rather than a path segment, which is
        how it escaped the narrowing the detail route also lacked.
        """
        response = self.get(
            self.bursar, "reports/customer-statement/", self.books,
            customer=self.lekki_family.code,
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_her_own_branchs_family_still_resolves(self):
        response = self.get(
            self.bursar, f"customers/{self.ikeja_family.code}/", self.books,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["customer"]["code"], self.ikeja_family.code)

    def test_a_school_wide_family_still_resolves_for_a_pinned_bursar(self):
        """The rule the inclusive form exists for.

        A family the school bills centrally has no branch. Hiding it would look
        like a missing record to everyone but the one person who could see it.
        """
        response = self.get(
            self.bursar, f"customers/{self.shared_family.code}/", self.books,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["customer"]["code"], self.shared_family.code)

    def test_an_unbound_head_reaches_every_branchs_family(self):
        for family in (self.ikeja_family, self.lekki_family, self.shared_family):
            with self.subTest(customer=family.code):
                response = self.get(
                    self.head, f"customers/{family.code}/", self.books,
                )
                self.assertEqual(response.status_code, 200, response.data)

    def test_a_single_branch_school_is_unaffected(self):
        """One branch means the dimension should recede, not start refusing.

        Both the pinned family and the school-wide one must resolve: a school
        that has never used a second branch should not be able to tell that any
        of this was added.
        """
        for family in (self.solo_family, self.solo_shared_family):
            with self.subTest(customer=family.code):
                response = self.get(
                    self.solo_bursar, f"customers/{family.code}/", self.solo_books,
                )
                self.assertEqual(response.status_code, 200, response.data)


class FeeStructureByReferenceTests(_ReferenceFixture):
    """A fee structure is the price list, so reaching one is reaching the prices."""

    def test_a_pinned_bursar_cannot_read_another_branchs_price_list(self):
        response = self.get(
            self.bursar, f"fee-structures/{self.lekki_fees.code}/", self.books,
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_a_pinned_bursar_cannot_rename_another_branchs_price_list(self):
        response = self.patch(
            self.bursar, f"fee-structures/{self.lekki_fees.code}/", self.books,
            {"name": "Renamed By Ikeja"},
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.lekki_fees.refresh_from_db()
        self.assertNotEqual(self.lekki_fees.name, "Renamed By Ikeja")

    def test_a_pinned_bursar_cannot_deactivate_another_branchs_price_list(self):
        """Worth its own case: a deactivation stops that branch billing at all."""
        response = self.patch(
            self.bursar, f"fee-structures/{self.lekki_fees.code}/", self.books,
            {"is_active": False},
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.lekki_fees.refresh_from_db()
        self.assertTrue(self.lekki_fees.is_active)

    def test_a_pinned_bursar_cannot_bill_from_another_branchs_price_list(self):
        """The worst of the four: it raises real debt against real families."""
        response = self.post(
            self.bursar, f"fee-structures/{self.lekki_fees.code}/generate/", self.books,
            {"invoice_date": "2026-01-15"},
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_a_pinned_bursar_cannot_clone_another_branchs_price_list(self):
        response = self.post(
            self.bursar, f"fee-structures/{self.lekki_fees.code}/duplicate/", self.books,
            {"code": "STOLEN"},
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(
            FeeStructure.objects.filter(entity=self.books, code="STOLEN").exists(),
        )

    def test_a_school_wide_price_list_still_resolves_for_a_pinned_bursar(self):
        """Corona publishes one template for all three branches. She uses it."""
        response = self.get(
            self.bursar, f"fee-structures/{self.shared_fees.code}/", self.books,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["code"], self.shared_fees.code)

    def test_an_unbound_head_reaches_both(self):
        for structure in (self.lekki_fees, self.shared_fees):
            with self.subTest(structure=structure.code):
                response = self.get(
                    self.head, f"fee-structures/{structure.code}/", self.books,
                )
                self.assertEqual(response.status_code, 200, response.data)


class TenantBoundaryUnchangedTests(_ReferenceFixture):
    """None of the branch work may weaken the tenant boundary it sits inside."""

    def test_a_rival_schools_customer_is_still_unreachable(self):
        rival_family = self.customer(self.rival_books, "RIVAL", self.rival_branch)
        response = self.get(
            self.head, f"customers/{rival_family.code}/", self.books,
        )

        self.assertEqual(response.status_code, 404, response.data)
        self.assertTrue(
            Customer.objects.filter(entity=self.rival_books, code="RIVAL").exists(),
            "the row still exists; it is simply not reachable from this tenant",
        )

    def test_an_unbound_caller_adds_no_branch_clause_at_all(self):
        """The whole-tenant caller must keep byte-identical SQL.

        ``BranchScope.filter`` returns the queryset untouched rather than adding
        a tautological term, and this is the assertion that keeps it that way -
        a regression here is a performance cliff on every finance read, not a
        correctness bug, so nothing else would catch it.
        """
        from vs_rbac.scoping import branch_q

        request = type("R", (), {"user": self.head.acting_user})()
        self.assertEqual(len(branch_q(request, include_shared=True)), 0)
