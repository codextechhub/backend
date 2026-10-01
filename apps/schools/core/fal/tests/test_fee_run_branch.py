"""Which branch each invoice of a fee run names, and who may raise it.

Every invoice names one real branch. A pupil is billed in the branch they
attend on the roll; a family with no pupil behind it gives its account's branch;
one shared by every branch takes the raiser's, which a school-wide raiser at a
school with several branches must name. Corona runs Ikeja and Lekki, Greenfield
runs Main alone, and both shapes are exercised.
"""
from __future__ import annotations

import datetime

from django.test import SimpleTestCase
from vs_config.clock import branch_today

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


def _net(account, branch, counterparty=None):
    """Debits less credits on ``account`` in one branch's books, against one counterparty."""
    from vs_finance.branch_ledger import ledger_lines

    lines = ledger_lines().filter(account=account, entry__branch=branch)
    if counterparty is not None:
        lines = lines.filter(counterparty_branch=counterparty)
    return sum(line.debit - line.credit for line in lines)


class MoveKeyTests(SimpleTestCase):
    """One key per pupil, branch pair, structure and period, the same every time it is asked."""

    def test_the_key_is_stable_and_changes_with_each_part(self):
        from types import SimpleNamespace

        from schools.core.fal.adapters.django_finance import _move_key

        tuition, bus = SimpleNamespace(pk=12), SimpleNamespace(pk=13)
        key = _move_key(tuition, "S4-T9", 44, 1, 2)

        self.assertEqual(key, "fee-run:F12:S4-T9:C44:B1-B2")
        self.assertEqual(key, _move_key(tuition, "S4-T9", 44, 1, 2))
        self.assertEqual(len({
            key, _move_key(bus, "S4-T9", 44, 1, 2), _move_key(tuition, "S4-T10", 44, 1, 2),
            _move_key(tuition, "S4-T9", 45, 1, 2), _move_key(tuition, "S4-T9", 44, 3, 2),
        }), 5)


class AccountFollowsPupilTests(RouteFixture):
    """Tunde attends Lekki on the roll while their fee account is filed at Ikeja.

    First Term (N150,000) was billed while Tunde attended Ikeja, and Tunde also
    holds a N5,000 receipt at Ikeja not yet applied to anything. The roll now
    says Lekki, so Second Term bills them at Lekki, their account moves to Lekki
    in the same transaction, and their whole open balance goes with it: the
    First Term bill becomes Lekki's to collect, the N5,000 follows as credit at
    Lekki, and Lekki owes Ikeja, through the inter-branch account, for what
    Ikeja had earned. Amaka attends Ikeja throughout and is billed there.
    """

    FIRST_TERM = 15_000_000
    CREDIT = 500_000

    def setUp(self):
        from schools.vs_academics.models import AcademicTerm
        from vs_finance.models import Customer, FeeItem

        super().setUp()
        self.as_bursar("finance.feestructure.edit", "finance.feestructure.generate")
        self.link()
        FeeItem.objects.filter(structure=self.structure).update(amount=self.FIRST_TERM)
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
        self.receipt(self.CREDIT)
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

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def move_to(student, branch):
        from schools.vs_students.models import Student

        Student.all_objects.filter(pk=student.pk).update(branch=branch)

    def receipt(self, amount, *, invoice=None):
        """A posted Ikeja receipt from Tunde's family, applied to ``invoice`` or left as credit."""
        from vs_finance.models import Payment
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity_id=self.books.entity_ref, customer=self.account, branch=self.ikeja,
            payment_date=branch_today(self.corona.tenant, self.ikeja), amount=amount,
            deposit_account=self.account_for("1100"),
        )
        post_payment(
            payment, allocations=[(invoice, amount)] if invoice is not None else None,
            auto_allocate=False,
        )
        return payment

    def account_for(self, code):
        """A ledger account by code (``self.account`` is Tunde's fee account here)."""
        return RouteFixture.account(self.books.entity_ref, code)

    def unearned_first_term(self):
        """First Term income not yet earned on the run's date, which moves with the bill."""
        from vs_finance.models import DeferredIncomeEntry

        return sum(entry.open_amount for entry in DeferredIncomeEntry.objects.filter(
            invoice=self.first_term_bill, status="PENDING",
            recognition_date__gt=branch_today(self.corona.tenant, None),
        ))

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

    def moves(self):
        from vs_finance.models import InterBranchTransfer

        return InterBranchTransfer.objects.filter(entity_id=self.books.entity_ref)

    def expected_move(self, *, owed=FIRST_TERM, invoices=1, deferred=0):
        return {
            "customer": self.account.pk, "student": str(self.tunde.pk),
            "name": "Tunde Adeyemi",
            "from_branch": "Ikeja", "from_branch_id": self.ikeja.pk,
            "to_branch": "Lekki", "to_branch_id": self.lekki.pk,
            "amount": owed - self.CREDIT, "invoice_count": invoices,
            "debit_note_count": 0, "credit_amount": self.CREDIT,
            "deferred_amount": deferred,
        }

    # ---- the move ----------------------------------------------------------
    def test_the_pupil_is_billed_where_they_attend_and_their_account_follows(self):
        from vs_finance.constants import FinanceAuditAction
        from vs_finance.models import FinanceAuditLog

        unearned = self.unearned_first_term()
        res = self.bill_second_term(self.tunde, self.amaka)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            self.second_term_bills(),
            {self.tunde.pk: self.lekki.pk, self.amaka.pk: self.ikeja.pk},
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.lekki.pk)
        self.assertEqual(
            res.data["data"]["accounts_moved"], [self.expected_move(deferred=unearned)],
        )

        entries = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CUSTOMER_UPDATED, target_id=str(self.account.pk),
        )
        self.assertEqual(
            sorted(entries.values_list("branch_id", flat=True)),
            sorted([self.ikeja.pk, self.lekki.pk]),
        )
        move = self.moves().get()
        for entry in entries:
            self.assertEqual(entry.before, {"branch_id": self.ikeja.pk})
            self.assertEqual(entry.after, {"branch_id": self.lekki.pk})
            self.assertEqual(entry.actor_id, self.bursar.pk)
            self.assertEqual(
                {k: entry.metadata[k] for k in
                 ("from_branch", "to_branch", "amount", "receivable_move_id")},
                {"from_branch": "Ikeja", "to_branch": "Lekki",
                 "amount": self.FIRST_TERM - self.CREDIT, "receivable_move_id": move.pk},
            )
            self.assertNotIn("deferred", str(entry.metadata))
        self.assertIn(
            "moved to Lekki, where the pupil attends, with ₦145,000.00 owed.",
            entries.get(branch=self.ikeja).message,
        )
        self.assertIn(
            "moved from Ikeja with ₦145,000.00 owed", entries.get(branch=self.lekki).message,
        )

    def test_the_first_term_bill_moves_to_the_branch_the_pupil_attends(self):
        """Lekki collects it now; the revenue it booked stays in Ikeja's journal.

        The N5,000 arrives at Lekki as credit and the school applies credit to
        open bills, so it settles part of First Term there.
        """
        self.bill_second_term(self.tunde)

        self.first_term_bill.refresh_from_db()
        self.assertEqual(self.first_term_bill.branch_id, self.lekki.pk)
        self.assertEqual(self.first_term_bill.balance_due, self.FIRST_TERM - self.CREDIT)
        self.assertEqual(self.first_term_bill.journal.branch_id, self.ikeja.pk)
        move = self.moves().get()
        self.assertEqual(
            (move.branch_id, move.to_branch_id, move.purpose),
            (self.ikeja.pk, self.lekki.pk, "Fee run: pupil attends Lekki"),
        )

    def test_each_branch_books_its_side_and_both_balance(self):
        """Ikeja is owed by Lekki; Lekki holds Tunde's receivable and owes Ikeja.

        Ikeja hands over N150,000 of receivable and N5,000 of credit, and any
        First Term income not yet earned, so Lekki owes Ikeja the difference.
        """
        from vs_finance.branch_ledger import ledger_lines

        ar, credit = self.account_for("1200"), self.account_for("2140")
        ib, deferred_income = self.account_for("1260"), self.account_for("2160")
        ikeja_ar = _net(ar, self.ikeja)
        unearned = self.unearned_first_term()

        res = self.bill_second_term(self.tunde)

        self.assertEqual(res.status_code, 201, res.data)
        owed_to_ikeja = self.FIRST_TERM - self.CREDIT - unearned
        self.assertEqual(_net(ar, self.ikeja), ikeja_ar - self.FIRST_TERM)
        self.assertEqual(_net(credit, self.ikeja), 0)
        self.assertEqual(_net(ib, self.ikeja, self.lekki), owed_to_ikeja)
        self.assertEqual(_net(ib, self.lekki, self.ikeja), -owed_to_ikeja)
        self.assertEqual(_net(ib, self.ikeja) + _net(ib, self.lekki), 0)
        second_term = _invoices(self.books).get(reference="FEE:JSS1-T2").total
        self.assertEqual(
            _net(ar, self.lekki) + _net(credit, self.lekki),
            self.FIRST_TERM + second_term - self.CREDIT,
        )
        self.assertEqual(_net(deferred_income, self.lekki), -(second_term + unearned))
        for branch in (self.ikeja, self.lekki):
            lines = ledger_lines().filter(entry__branch=branch)
            self.assertEqual(
                sum(line.debit for line in lines), sum(line.credit for line in lines), branch.name,
            )

    def test_income_not_yet_earned_moves_with_the_bill(self):
        """Ikeja billed Tunde's Second Term bus fare in advance: Lekki will earn it.

        N60,000 for January to March 2027, all of it still deferred at Ikeja.
        It moves with the bill, so Ikeja's deferred income for it is cleared and
        Lekki's carries it. Posting the bus bill spends Tunde's N5,000 credit on
        it, so no credit is left to move.
        """
        from vs_finance.models import Invoice, InvoiceLine
        from vs_finance.receivables import post_invoice

        bus = Invoice.objects.create(
            entity_id=self.books.entity_ref, customer=self.account, branch=self.ikeja,
            invoice_date=branch_today(self.corona.tenant, self.ikeja),
            due_date=datetime.date(2027, 1, 6),
        )
        InvoiceLine.objects.create(
            invoice=bus, line_no=1, quantity=1, unit_price=6_000_000,
            revenue_account=self.account_for("4100"),
            service_start=datetime.date(2027, 1, 6), service_end=datetime.date(2027, 3, 31),
        )
        post_invoice(bus)
        deferred_income = self.account_for("2160")
        ikeja_deferred = _net(deferred_income, self.ikeja)
        unearned = self.unearned_first_term() + 6_000_000

        res = self.bill_second_term(self.tunde)

        self.assertEqual(res.status_code, 201, res.data)
        expected = self.expected_move(
            owed=self.FIRST_TERM + 6_000_000, invoices=2, deferred=unearned)
        expected["credit_amount"] = 0
        self.assertEqual(res.data["data"]["accounts_moved"], [expected])
        bus.refresh_from_db()
        self.assertEqual(bus.branch_id, self.lekki.pk)
        self.assertEqual(_net(deferred_income, self.ikeja), ikeja_deferred + unearned)
        self.assertEqual(
            _net(self.account_for("1260"), self.ikeja, self.lekki),
            self.FIRST_TERM + 6_000_000 - self.CREDIT - unearned,
        )

    def test_a_pupil_in_credit_takes_the_credit_with_them(self):
        """Tunde paid First Term in full and still holds the N5,000: Ikeja owes Lekki."""
        from vs_finance.models import Payment

        self.receipt(self.FIRST_TERM, invoice=self.first_term_bill)
        unearned = self.unearned_first_term()

        res = self.bill_second_term(self.tunde)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            res.data["data"]["accounts_moved"],
            [self.expected_move(owed=0, invoices=1 if unearned else 0, deferred=unearned)],
        )
        self.assertEqual(res.data["data"]["accounts_moved"][0]["amount"], -self.CREDIT)
        self.assertEqual(
            _net(self.account_for("1260"), self.ikeja, self.lekki), -(self.CREDIT + unearned),
        )
        self.assertTrue(Payment.objects.filter(
            customer=self.account, branch=self.lekki, method="CREDIT_TRANSFER",
            amount=self.CREDIT,
        ).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.lekki.pk)
        messages = self.move_messages(self.audit_reader("audit-all@corona.test"))
        self.assertEqual(len(messages), 2, messages)
        self.assertTrue(all("with ₦5,000.00 in credit" in m for m in messages), messages)

    # ---- the audit trail ---------------------------------------------------
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
        self.assertIn("₦145,000.00 owed", ikeja[0])
        self.assertEqual(len(lekki), 1, lekki)
        self.assertIn("moved from Ikeja with ₦145,000.00 owed", lekki[0])
        self.assertEqual(sorted(school), sorted(ikeja + lekki))

    # ---- reruns and previews -----------------------------------------------
    def test_a_rerun_after_the_move_bills_nobody_twice_and_moves_nobody(self):
        self.bill_second_term(self.tunde, self.amaka)
        ikeja_ib = _net(self.account_for("1260"), self.ikeja)
        again = self.bill_second_term(self.tunde, self.amaka)

        self.assertEqual(again.status_code, 201, again.data)
        self.assertEqual(again.data["data"]["counts"]["created"], 0)
        self.assertEqual(again.data["data"]["counts"]["skipped"], 2)
        self.assertEqual(again.data["data"]["accounts_moved"], [])
        self.assertEqual(_invoices(self.books).filter(reference="FEE:JSS1-T2").count(), 2)
        self.assertEqual(self.moves().count(), 1)
        self.assertEqual(_net(self.account_for("1260"), self.ikeja), ikeja_ib)

    def test_a_run_queued_behind_the_move_finds_the_account_moved_and_moves_nothing(self):
        """Two runs read Tunde's account at Ikeja; the second waits on its lock and finds it at Lekki."""
        from schools.core.fal.adapters.django_finance import _refile_accounts
        from vs_finance.models import Customer

        stale = Customer.objects.get(pk=self.account.pk)
        self.bill_second_term(self.tunde)

        moved = _refile_accounts(
            [stale], {stale.pk: self.lekki.pk}, actor_user=self.bursar,
            structure=self.second, period_key="S0-T0",
            on=branch_today(self.corona.tenant, None),
        )

        self.assertEqual(moved, ())
        self.assertEqual(stale.branch_id, self.lekki.pk)
        self.assertEqual(self.moves().count(), 1)

    def test_a_rerun_of_the_term_already_billed_skips_them_and_moves_nothing(self):
        """First Term again, after the roll changed: Tunde was billed for it at Ikeja."""
        again = self.post(self.gen_url(), {"students": [str(self.tunde.pk)]})

        self.assertEqual(again.status_code, 201, again.data)
        self.assertEqual(again.data["data"]["counts"]["created"], 0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)
        self.assertFalse(self.moves().exists())

    def test_a_preview_lists_the_figures_and_moves_nothing(self):
        from vs_finance.branch_ledger import ledger_lines
        from vs_finance.models import Payment

        lines = ledger_lines().count()
        unearned = self.unearned_first_term()

        res = self.bill_second_term(self.tunde, dry_run=True)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["data"]["accounts_moved"], [self.expected_move(deferred=unearned)],
        )
        self.account.refresh_from_db()
        self.first_term_bill.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)
        self.assertEqual(self.first_term_bill.branch_id, self.ikeja.pk)
        self.assertFalse(self.moves().exists())
        self.assertFalse(Payment.objects.filter(branch=self.lekki).exists())
        self.assertEqual(ledger_lines().count(), lines)

        real = self.bill_second_term(self.tunde)
        self.assertEqual(real.data["data"]["accounts_moved"], res.data["data"]["accounts_moved"])

    # ---- reach -------------------------------------------------------------
    def invoice_reader(self, email, branch):
        user = self.user_for(self.corona, email, branch=branch)
        self.grant(user, "finance.invoice.view", branch=branch)
        return self.client_for(user)

    def sees_first_term(self, client):
        res = client.get(
            f"/v1/finance/invoices/{self.first_term_bill.pk}/?entity=CORONA&tenant={self.slug}"
        )
        self.assertIn(res.status_code, (200, 404), res.data)
        return res.status_code == 200

    def test_a_lekki_bursar_bills_a_pupil_who_attends_lekki_and_moves_the_account(self):
        """Afterwards Lekki's bursar finds First Term on their books and Ikeja's does not."""
        self.grant(self.lekki_bursar, "finance.invoice.view", branch=self.lekki)
        lekki_reader = self.client_for(self.lekki_bursar)
        ikeja_reader = self.invoice_reader("invoices-ikeja@corona.test", self.ikeja)
        self.assertEqual(
            (self.sees_first_term(lekki_reader), self.sees_first_term(ikeja_reader)),
            (False, True),
        )
        self.as_lekki_bursar()

        res = self.bill_second_term(self.tunde)

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.second_term_bills(), {self.tunde.pk: self.lekki.pk})
        self.account.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.lekki.pk)
        self.assertEqual(
            (self.sees_first_term(lekki_reader), self.sees_first_term(ikeja_reader)),
            (True, False),
        )

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
        self.first_term_bill.refresh_from_db()
        self.assertEqual(self.account.branch_id, self.ikeja.pk)
        self.assertEqual(self.first_term_bill.branch_id, self.ikeja.pk)
        self.assertEqual(self.second_term_bills(), {})
        self.assertFalse(self.moves().exists())

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
        self.assertFalse(self.moves().exists())
