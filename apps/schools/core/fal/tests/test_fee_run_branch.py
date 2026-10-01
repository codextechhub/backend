"""Which branch each invoice of a fee run names, and who may raise it.

Every invoice names one real branch. A pupil is billed in the branch they
attend on the roll; a family with no pupil behind it gives its account's branch;
one shared by every branch takes the raiser's, which a school-wide raiser at a
school with several branches must name. Corona runs Ikeja and Lekki, Greenfield
runs Main alone, and both shapes are exercised.
"""
from __future__ import annotations

import datetime

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


class AccountFollowsPupilTests(RouteFixture):
    """Tunde attends Lekki on the roll while their fee account is filed at Ikeja.

    First Term was billed while Tunde attended Ikeja. The roll now says Lekki,
    so Second Term bills them at Lekki and their account moves to Lekki in the
    same transaction; the First Term bill stays Ikeja's. Amaka attends Ikeja
    throughout and is billed there in the same run.
    """

    def setUp(self):
        from schools.vs_academics.models import AcademicTerm
        from vs_finance.models import Customer

        super().setUp()
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.link()
        self.tunde = self.student(self.corona, self.ikeja)
        self.amaka = self.student(self.corona, self.ikeja, first="Amaka")
        first = self.post(
            self.gen_url(), {"students": [str(self.tunde.pk), str(self.amaka.pk)]},
        )
        self.assertEqual(first.status_code, 201, first.data)
        self.first_term_bill = _invoices(self.books).get(
            customer__source_id=str(self.tunde.pk))
        self.account = Customer.objects.get(
            entity_id=self.books.entity_ref, source_id=str(self.tunde.pk))
        self.move_to(self.tunde, self.lekki)

        second_term = AcademicTerm.all_objects.create(
            tenant=self.corona.tenant, session=self.session, name="Second Term",
            order_index=2, start_date=datetime.date(2027, 1, 6),
            end_date=datetime.date(2027, 4, 2),
        )
        self.second = self.fee_structure(self.books, code="JSS1-T2")
        res = self.post(
            self.link_url(self.second.pk), {"session": self.session.pk, "term": second_term.pk},
        )
        self.assertEqual(res.status_code, 200, res.data)

    @staticmethod
    def move_to(student, branch):
        from schools.vs_students.models import Student

        Student.all_objects.filter(pk=student.pk).update(branch=branch)

    def bill_second_term(self, *students, dry_run=False):
        return self.post(self.gen_url(self.second.pk), {
            "students": [str(s.pk) for s in students], "dry_run": dry_run,
        })

    def second_term_bills(self):
        return {
            int(row["customer__source_id"]): row["branch_id"]
            for row in _invoices(self.books).filter(reference="FEE:JSS1-T2")
            .values("customer__source_id", "branch_id")
        }

    def test_the_pupil_is_billed_where_they_attend_and_their_account_follows(self):
        from vs_finance.constants import FinanceAuditAction
        from vs_finance.models import FinanceAuditLog

        res = self.bill_second_term(self.tunde, self.amaka)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            self.second_term_bills(),
            {self.tunde.pk: self.lekki.pk, self.amaka.pk: self.ikeja.pk},
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.lekki.pk)
        self.assertEqual(res.data["data"]["accounts_moved"], [{
            "customer": self.account.pk, "student": str(self.tunde.pk),
            "name": "Tunde Adeyemi",
            "from_branch": "Ikeja", "from_branch_id": self.ikeja.pk,
            "to_branch": "Lekki", "to_branch_id": self.lekki.pk,
        }])

        entries = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CUSTOMER_UPDATED, target_id=str(self.account.pk),
        )
        self.assertEqual(
            sorted(entries.values_list("branch_id", flat=True)),
            sorted([self.ikeja.pk, self.lekki.pk]),
        )
        for entry in entries:
            self.assertEqual(entry.before, {"branch_id": self.ikeja.pk})
            self.assertEqual(entry.after, {"branch_id": self.lekki.pk})
            self.assertEqual(entry.actor_id, self.bursar.pk)
            self.assertEqual(
                (entry.metadata["from_branch"], entry.metadata["to_branch"]),
                ("Ikeja", "Lekki"),
            )
        self.assertIn("moved to Lekki", entries.get(branch=self.ikeja).message)
        self.assertIn("moved from Ikeja", entries.get(branch=self.lekki).message)

    def audit_reader(self, email, branch=None):
        user = self.user_for(self.corona, email)
        self.grant(user, "finance.audit.view", branch=branch)
        return self.client_for(user)

    def move_messages(self, client):
        res = client.get(
            f"/v1/finance/audit-logs/?entity=CORONA&page_size=100&tenant={self.slug}"
            f"&action=CUSTOMER_UPDATED"
        )
        self.assertEqual(res.status_code, 200, res.data)
        return [
            row["message"] for row in res.data["data"]
            if row["target_id"] == str(self.account.pk)
        ]

    def test_each_branch_reads_its_own_side_of_the_move(self):
        """Ikeja reads why Tunde left; Lekki reads that they arrived; the school both."""
        self.bill_second_term(self.tunde)

        ikeja = self.move_messages(self.audit_reader("audit-ikeja@corona.test", self.ikeja))
        lekki = self.move_messages(self.audit_reader("audit-lekki@corona.test", self.lekki))
        school = self.move_messages(self.audit_reader("audit-all@corona.test"))

        self.assertEqual(len(ikeja), 1, ikeja)
        self.assertIn("moved to Lekki", ikeja[0])
        self.assertEqual(len(lekki), 1, lekki)
        self.assertIn("moved from Ikeja", lekki[0])
        self.assertEqual(sorted(school), sorted(ikeja + lekki))

    def test_the_old_bill_stays_with_the_branch_that_raised_it(self):
        self.bill_second_term(self.tunde)

        self.first_term_bill.refresh_from_db()
        self.assertEqual(self.first_term_bill.branch_id, self.ikeja.pk)

    def test_a_rerun_after_the_move_bills_nobody_twice_and_moves_nobody(self):
        self.bill_second_term(self.tunde, self.amaka)
        again = self.bill_second_term(self.tunde, self.amaka)

        self.assertEqual(again.status_code, 201, again.data)
        self.assertEqual(again.data["data"]["counts"]["created"], 0)
        self.assertEqual(again.data["data"]["counts"]["skipped"], 2)
        self.assertEqual(again.data["data"]["accounts_moved"], [])
        self.assertEqual(_invoices(self.books).filter(reference="FEE:JSS1-T2").count(), 2)

    def test_a_rerun_of_the_term_already_billed_skips_them_and_moves_nothing(self):
        """First Term again, after the move: Tunde was billed for it at Ikeja."""
        again = self.post(self.gen_url(), {"students": [str(self.tunde.pk)]})

        self.assertEqual(again.status_code, 201, again.data)
        self.assertEqual(again.data["data"]["counts"]["created"], 0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)

    def test_a_preview_lists_the_move_and_makes_none(self):
        res = self.bill_second_term(self.tunde, dry_run=True)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["data"]["accounts_moved"]), 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)

    def test_a_lekki_bursar_bills_a_pupil_who_attends_lekki_and_moves_the_account(self):
        self.as_lekki_bursar()

        res = self.bill_second_term(self.tunde)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.second_term_bills(), {self.tunde.pk: self.lekki.pk})
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.lekki.pk)

    # ---- reach -------------------------------------------------------------
    def yaba_bursar(self):
        from vs_tenants.models import Branch

        yaba = Branch.objects.create(
            tenant=self.corona.tenant, name="Yaba", is_main=False, status="ACTIVE",
        )
        user = self.user_for(self.corona, "yaba@corona.test")
        for key in ("finance.feestructure.edit", "finance.feestructure.generate"):
            self.grant(user, key, branch=yaba)
        return user

    def assertNothingMoved(self):
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)
        self.assertEqual(self.second_term_bills(), {})

    def test_a_bursar_of_neither_branch_cannot_bill_or_move_them(self):
        user = self.yaba_bursar()

        with self.assertRaises(CrossBranchError):
            DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
                self.second.pk, (str(self.tunde.pk),), raiser_ref=user.pk,
            )
        self.as_bursar(user=user)
        res = self.bill_second_term(self.tunde)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertNothingMoved()

    def test_a_lekki_bursar_cannot_pull_a_pupil_who_does_not_attend_lekki(self):
        """Amaka attends Ikeja; their account was re-filed at Lekki by hand.

        The account being Lekki's gives the Lekki bursar nothing: the roll says
        Ikeja, so naming Amaka is refused and nothing moves or is billed.
        """
        from vs_finance.models import Customer

        amaka_account = Customer.objects.get(
            entity_id=self.books.entity_ref, source_id=str(self.amaka.pk))
        Customer.objects.filter(pk=amaka_account.pk).update(branch=self.lekki)

        with self.assertRaises(CrossBranchError):
            DjangoFeeTermBridgeAdapter().generate_cohort_invoices(
                self.second.pk, (str(self.amaka.pk),), raiser_ref=self.lekki_bursar.pk,
            )
        self.as_lekki_bursar()
        res = self.bill_second_term(self.amaka)
        self.assertEqual(res.status_code, 404, res.data)
        amaka_account.refresh_from_db()
        self.assertEqual(amaka_account.branch_id, self.lekki.pk)
        self.assertEqual(self.second_term_bills(), {})
