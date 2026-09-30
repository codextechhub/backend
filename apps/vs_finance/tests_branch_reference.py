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


class FeeRunBillsOnlyWhatTheCallerReachesTests(_ReferenceFixture):
    """Billing from a fee structure raises debt, so who is billed is the whole question.

    Two bounds, and each is asserted for both forms of the body (``all_active``
    and a named list): the caller's branch reach, and the structure's own
    branch. Corona's Ikeja bursar reaches Ikeja's families and the ones every
    branch shares, whose bills she raises at Ikeja; a Lekki price list bills
    Lekki's families and nobody else's.
    """

    def setUp(self):
        super().setUp()
        from vs_finance.models import Account, FeeItem

        revenue = Account.objects.get(entity=self.books, code="4100")
        for structure in (self.lekki_fees, self.shared_fees):
            FeeItem.objects.create(
                structure=structure, line_no=1, description="Tuition",
                revenue_account=revenue, amount=5_000_000,
            )

    def run_fees(self, client, structure, body, books=None):
        return self.post(
            client, f"fee-structures/{structure.code}/generate/", books or self.books,
            {"invoice_date": "2026-01-15", **body},
        )

    def billed(self, structure):
        from vs_finance.models import Invoice

        return set(
            Invoice.objects.filter(reference=f"FEE:{structure.code}")
            .values_list("customer__code", flat=True)
        )

    def test_a_pinned_bursars_all_active_run_bills_only_her_reach(self):
        """Ikeja's family and the school-wide one; Lekki's family is not hers to bill."""
        response = self.run_fees(self.bursar, self.shared_fees, {"all_active": True})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.billed(self.shared_fees),
            {self.ikeja_family.code, self.shared_family.code},
        )

    def test_naming_another_branchs_family_answers_as_an_unknown_code_does(self):
        """404, not 403, and nothing is billed, not even the lines she could reach."""
        response = self.run_fees(
            self.bursar, self.shared_fees,
            {"customers": [self.ikeja_family.code, self.lekki_family.code]},
        )
        invented = self.run_fees(self.bursar, self.shared_fees, {"customers": ["NOSUCH"]})

        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(invented.status_code, 404, invented.data)
        self.assertEqual(
            str(response.data["message"]).replace(self.lekki_family.code, "X"),
            str(invented.data["message"]).replace("NOSUCH", "X"),
        )
        self.assertEqual(self.billed(self.shared_fees), set())

    def test_a_whole_school_callers_all_active_run_names_the_shared_familys_branch(self):
        """Each family is billed at its own branch; the shared one where the run says."""
        from vs_finance.models import Invoice

        unnamed = self.run_fees(self.head, self.shared_fees, {"all_active": True})
        self.assertEqual(unnamed.status_code, 400, unnamed.data)
        self.assertEqual(self.billed(self.shared_fees), set())

        response = self.run_fees(
            self.head, self.shared_fees, {"all_active": True, "branch": self.lekki.pk})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.billed(self.shared_fees),
            {self.ikeja_family.code, self.lekki_family.code, self.shared_family.code},
        )
        self.assertEqual(
            Invoice.objects.get(reference=f"FEE:{self.shared_fees.code}",
                                customer=self.shared_family).branch_id,
            self.lekki.pk,
        )

    def test_a_branch_price_list_refuses_a_family_filed_at_another_branch(self):
        """The FAL cohort route's rule, held on the engine's own route too."""
        response = self.run_fees(
            self.head, self.lekki_fees,
            {"customers": [self.lekki_family.code, self.ikeja_family.code]},
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data.get("code"), "WRONG_BRANCH")
        self.assertEqual(self.billed(self.lekki_fees), set())

    def test_a_branch_price_list_refuses_a_school_wide_family(self):
        """Its bill would be filed school-wide at Lekki's prices."""
        response = self.run_fees(
            self.head, self.lekki_fees, {"customers": [self.shared_family.code]},
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data.get("code"), "WRONG_BRANCH")

    def test_a_branch_price_list_bills_its_own_branchs_family(self):
        response = self.run_fees(
            self.head, self.lekki_fees, {"customers": [self.lekki_family.code]},
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.billed(self.lekki_fees), {self.lekki_family.code})

    def test_all_active_from_a_branch_price_list_bills_only_that_branch(self):
        """Selected, not refused: every other branch's family simply is not in the run."""
        response = self.run_fees(self.head, self.lekki_fees, {"all_active": True})

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.billed(self.lekki_fees), {self.lekki_family.code})

    def test_a_single_branch_school_bills_every_family(self):
        from vs_finance.models import Account, FeeItem

        solo_fees = self.fee_structure(self.solo_books, "SOLOFEE", None)
        FeeItem.objects.create(
            structure=solo_fees, line_no=1, description="Tuition",
            revenue_account=Account.objects.get(entity=self.solo_books, code="4100"),
            amount=5_000_000,
        )

        response = self.run_fees(
            self.solo_bursar, solo_fees, {"all_active": True}, books=self.solo_books,
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.billed(solo_fees),
            {self.solo_family.code, self.solo_shared_family.code},
        )


class BulkRunsNarrowToTheCallersReachTests(_ReferenceFixture):
    """The other AR routes that act on many rows at once, by the same two rules.

    A dunning run with no customer chases every overdue invoice it can see, and
    an adjustment batch resolves many references in one query. Each is a place a
    per-row resolver is not called, so each needs the narrowing of its own.
    """

    KEYS = _ReferenceFixture.KEYS + (
        "finance.dunning.generate", "finance.writeoff.create",
    )

    def setUp(self):
        super().setUp()
        from vs_finance.dunning import ensure_default_policy
        from vs_finance.models import Account
        from vs_finance.receivables import post_invoice

        ensure_default_policy(self.books)

        self.ikeja_bill = self.invoice(self.books, self.ikeja_family, self.ikeja)
        self.lekki_bill = self.invoice(self.books, self.lekki_family, self.lekki)
        self.shared_bill = self.invoice(self.books, self.shared_family, None)
        postable = Account.objects.get(entity=self.books, code="4100")
        for bill in (self.ikeja_bill, self.lekki_bill, self.shared_bill):
            bill.lines.update(revenue_account=postable)
            post_invoice(bill)
            bill.refresh_from_db()

    def chased(self):
        from vs_finance.models import DunningNotice

        return set(DunningNotice.objects.filter(entity=self.books)
                   .values_list("invoice_id", flat=True))

    def test_a_pinned_bursars_dunning_run_chases_only_her_reach(self):
        """Ikeja's bill only: the unbranched one is not hers to chase."""
        response = self.post(self.bursar, "dunning/generate/", self.books, {"as_of": "2026-01-31"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.chased(), {self.ikeja_bill.pk})

    def test_a_whole_school_callers_dunning_run_is_unchanged(self):
        response = self.post(self.head, "dunning/generate/", self.books, {"as_of": "2026-01-31"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.chased(),
            {self.ikeja_bill.pk, self.lekki_bill.pk, self.shared_bill.pk},
        )

    def test_a_write_off_batch_naming_another_branchs_invoice_is_not_found(self):
        """Refused as an unknown number is, before anything is drafted."""
        from vs_finance.models import WriteOffRequest

        response = self.post(self.bursar, "ar-adjustments/batch/", self.books, {
            "kind": "WRITEOFF", "action": "DRAFT", "date": "2026-01-31",
            "items": [{"invoice": self.lekki_bill.document_number}],
        })

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(WriteOffRequest.objects.filter(invoice=self.lekki_bill).exists())

    def test_a_write_off_batch_on_her_own_branchs_invoice_drafts(self):
        response = self.post(self.bursar, "ar-adjustments/batch/", self.books, {
            "kind": "WRITEOFF", "action": "DRAFT", "date": "2026-01-31",
            "items": [{"invoice": self.ikeja_bill.document_number}],
        })

        self.assertEqual(response.status_code, 201, response.data)
