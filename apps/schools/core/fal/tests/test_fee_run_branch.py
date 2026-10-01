"""Which branch each invoice of a fee run names, and who may raise it.

Every invoice names one real branch. A pupil is billed in the branch they
attend on the roll; a family with no pupil behind it gives its account's branch;
one shared by every branch takes the raiser's, which a school-wide raiser at a
school with several branches must name. Corona runs Ikeja and Lekki, Greenfield
runs Main alone, and both shapes are exercised.
"""
from __future__ import annotations

from schools.core.fal.adapters.django_finance import DjangoFeeTermBridgeAdapter
from schools.core.fal.exceptions import CrossBranchError

from .test_route import RouteFixture


def _invoices(books):
    from vs_finance.models import Invoice

    return Invoice.objects.filter(entity_id=books.entity_ref)


class FeeRunBranchSecurityTests(RouteFixture):
    """A branch-bound bursar bills their own branches' families and nobody else's."""

    def test_the_bridge_refuses_a_branch_bursar_billing_another_branchs_child(self):
        """The rule holds at the bridge, not only at the route in front of it."""
        self.as_lekki_bursar()
        self.link()
        ikeja_child = self.student(self.corona, self.ikeja)

        with self.assertRaises(CrossBranchError):
            DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
                self.structure.pk, (str(ikeja_child.pk),),
                raiser_ref=self.lekki_bursar.pk,
            )
        self.assertFalse(_invoices(self.books).exists())

    def test_a_branch_bursar_cannot_bill_an_imported_family_filed_at_another_branch(self):
        """The Okafors' account came in with the ledger before the roll, filed at Ikeja.

        No pupil stands behind the reference, so the route's roll check has
        nothing to look at; the run itself must refuse the Lekki bursar, with
        the 404 an unknown child gets.
        """
        self.as_lekki_bursar()
        self.link()
        self.student_customer(self.books, "IMP-OKAFOR", name="Okafor family",
                              branch=self.ikeja)

        res = self.post(self.gen_url(), {"students": ["IMP-OKAFOR"]})
        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(_invoices(self.books).exists())

    def test_a_branch_bursar_may_not_name_another_branch_for_a_shared_family(self):
        self.as_lekki_bursar()
        self.link()
        self.student_customer(self.books, "IMP-BELLO", name="Bello family")

        res = self.post(self.gen_url(), {"students": ["IMP-BELLO"], "branch": self.ikeja.pk})
        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(_invoices(self.books).exists())

    def test_a_branch_bursar_bills_a_shared_family_in_their_own_branch(self):
        self.as_lekki_bursar()
        self.link()
        self.student_customer(self.books, "IMP-BELLO", name="Bello family")

        res = self.post(self.gen_url(), {"students": ["IMP-BELLO"]})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            list(_invoices(self.books).values_list("branch_id", flat=True)), [self.lekki.pk],
        )

    def test_a_whole_school_run_bills_each_child_in_their_own_branch(self):
        """Tunde attends Ikeja and Amaka Lekki; Chidi's old account names no branch.

        Chidi's account was opened before accounts carried a branch, so it is
        shared by every branch. They attend Lekki, so their bill is Lekki's, never
        a bill with no branch.
        """
        from vs_finance.models import Customer

        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.link()
        tunde = self.student(self.corona, self.ikeja)
        amaka = self.student(self.corona, self.lekki, first="Amaka")
        chidi = self.student(self.corona, self.lekki, first="Chidi")
        legacy = self.student_customer(self.books, str(chidi.pk))
        Customer.objects.filter(pk=legacy.customer_ref).update(branch=None)

        res = self.post(
            self.gen_url(), {"students": [str(tunde.pk), str(amaka.pk), str(chidi.pk)]},
        )
        self.assertEqual(res.status_code, 201, res.data)
        by_child = {
            int(row["customer__source_id"]): row["branch_id"]
            for row in _invoices(self.books).values("customer__source_id", "branch_id")
        }
        self.assertEqual(by_child, {
            tunde.pk: self.ikeja.pk, amaka.pk: self.lekki.pk, chidi.pk: self.lekki.pk,
        })


class FeeRunBranchRequiredTests(RouteFixture):
    """A family shared by every branch is billed where somebody says."""

    def setUp(self):
        super().setUp()
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.link()
        self.student_customer(self.books, "IMP-BELLO", name="Bello family")

    def test_a_school_with_several_branches_asks_which_one(self):
        for dry_run in (True, False):
            res = self.post(self.gen_url(), {"students": ["IMP-BELLO"], "dry_run": dry_run})
            self.assertEqual(res.status_code, 400, res.data)
            self.assertEqual(res.data.get("code"), "BRANCH_REQUIRED")
            self.assertIn("branch", res.data["error"])
        self.assertFalse(_invoices(self.books).exists())

    def test_the_branch_named_is_the_one_billed(self):
        res = self.post(self.gen_url(), {"students": ["IMP-BELLO"], "branch": self.lekki.pk})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            list(_invoices(self.books).values_list("branch_id", flat=True)), [self.lekki.pk],
        )

    def test_another_schools_branch_is_refused_as_unknown(self):
        res = self.post(
            self.gen_url(), {"students": ["IMP-BELLO"], "branch": self.greenfield_main.pk},
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(_invoices(self.books).exists())

    def test_a_run_of_pupils_alone_needs_no_branch(self):
        child = self.student(self.corona, self.ikeja)
        res = self.post(self.gen_url(), {"students": [str(child.pk)]})
        self.assertEqual(res.status_code, 201, res.data)


class OneBranchFeeRunTests(RouteFixture):
    """Greenfield runs Main alone, so nobody is ever asked which branch."""

    def setUp(self):
        super().setUp()
        self.books = self.greenfield_books
        self.structure = self.fee_structure(self.books, code="GRN-TUITION")
        self.session, self.term = self.session_and_term(self.greenfield)
        self.as_bursar(
            "finance.feestructure.edit", "finance.feestructure.generate",
            user=self.greenfield_bursar,
        )
        self.link()

    def test_a_shared_family_is_billed_at_the_only_branch(self):
        self.student_customer(self.books, "IMP-ADE", name="Ade family")
        res = self.post(self.gen_url(), {"students": ["IMP-ADE"]})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            list(_invoices(self.books).values_list("branch_id", flat=True)),
            [self.greenfield_main.pk],
        )

    def test_a_pupil_is_billed_at_the_only_branch(self):
        child = self.student(self.greenfield, self.greenfield_main)
        res = self.post(self.gen_url(), {"students": [str(child.pk)]})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            list(_invoices(self.books).values_list("branch_id", flat=True)),
            [self.greenfield_main.pk],
        )


class AccountBranchConflictTests(RouteFixture):
    """Tunde's account is filed at Ikeja while the roll says they attend Lekki."""

    def setUp(self):
        super().setUp()
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.link()
        self.tunde = self.student(self.corona, self.ikeja)

    def move_to_lekki(self):
        from schools.vs_students.models import Student

        Student.all_objects.filter(pk=self.tunde.pk).update(branch=self.lekki)

    def test_a_rerun_after_a_move_bills_nobody_twice(self):
        """The billing key is per account, structure and period, never per branch."""
        first = self.post(self.gen_url(), {"students": [str(self.tunde.pk)]})
        self.assertEqual(first.status_code, 201, first.data)
        self.move_to_lekki()

        again = self.post(self.gen_url(), {"students": [str(self.tunde.pk)]})
        self.assertEqual(again.status_code, 201, again.data)
        self.assertEqual(again.data["data"]["counts"]["skipped"], 1)
        self.assertEqual(again.data["data"]["counts"]["created"], 0)
        self.assertEqual(_invoices(self.books).count(), 1)

    def test_a_new_bill_for_a_pupil_whose_account_is_at_another_branch_is_refused(self):
        self.student_customer(self.books, str(self.tunde.pk))
        self.move_to_lekki()

        for dry_run in (True, False):
            res = self.post(
                self.gen_url(), {"students": [str(self.tunde.pk)], "dry_run": dry_run},
            )
            self.assertEqual(res.status_code, 409, res.data)
            self.assertEqual(res.data.get("code"), "ACCOUNT_BRANCH_CONFLICT")
        self.assertFalse(_invoices(self.books).exists())
