"""One payroll run for all staff, one journal per branch, each paid from its own bank.

Corona runs one January payroll for Ikeja and Lekki. Ada teaches at Ikeja on
50,000 gross (net 45,000) and Bola at Lekki on 80,000 gross (net 72,000). The
run's salary cost is Ikeja's and Lekki's own, so posting books two accrual
journals, one naming each branch, and each branch's net wages leave that
branch's bank account. Cancelling reverses each journal on its own.

Mr Okon, a driver with no branch on their salary row, stops the run from posting
until somebody says whose driver they are. Harbour Primary, with one branch, posts
one journal exactly as before.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, PayrollRunStatus
from vs_finance.exceptions import PayrollBranchUnassignedError, PostingError
from vs_finance.models import (
    Account, BankAccount, EmployeeSalary, JournalEntry, PayrollLine, PayrollRun,
    PayrollRunBranch,
)

from .tests_branch_scope import _FinanceBranchFixture

JAN_25 = datetime.date(2026, 1, 25)


class _SplitFixture(_FinanceBranchFixture):
    KEYS = (
        "finance.payrollrun.create", "finance.payrollrun.view", "finance.payrollrun.post",
        "finance.payrollrun.pay",
    )

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import FinancePayrollSettings

        super().setUpTestData()
        # These schools supply their own PAYE and pension figures and switch the
        # other statutory items off, so each line is exactly the roster's typed
        # figures and the journals here are about branches, not statutory rules.
        for books in (cls.books, cls.solo_books):
            FinancePayrollSettings.objects.create(
                entity=books, paye_method="SUPPLIED", employer_pension_enabled=False,
                nhf_enabled=False, nsitf_enabled=False, itf_enabled=False,
            )
        cls.ada = cls.salary(cls.books, "Ada Obi", cls.ikeja, gross=50_000, paye=3_000, pension=2_000)
        cls.bola = cls.salary(cls.books, "Bola Lawal", cls.lekki, gross=80_000, paye=5_000, pension=3_000)
        cls.ikeja_bank = cls.bank(cls.books, "Ikeja Operations", cls.ikeja, "71")
        cls.lekki_bank = cls.bank(cls.books, "Lekki Operations", cls.lekki, "72")
        cls.bello_user = cls.user_holding(cls.tenant, "split-hq@corona.test")

    def setUp(self):
        super().setUp()
        self.bello = TenantAPIClient(user=self.bello_user)

    @classmethod
    def salary(cls, entity, name, branch, *, gross, paye=0, pension=0, employee=None):
        return EmployeeSalary.objects.create(
            entity=entity, name=name, branch=branch, employee=employee,
            gross_amount=gross, paye_amount=paye, pension_amount=pension,
        )

    @classmethod
    def bank(cls, entity, name, branch, tag):
        gl = Account.objects.create(
            entity=entity, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=entity, code="1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(entity=entity, name=name, branch=branch, gl_account=gl)

    @classmethod
    def user_holding(cls, tenant, email, branch=None):
        return cls.grant(
            cls.user_for(tenant, email), *cls.KEYS, tenant=tenant,
            role_key=f"role-{email}", branch=branch,
        )

    def client_for(self, tenant, email, branch=None):
        return TenantAPIClient(user=self.user_holding(tenant, email, branch=branch))

    def generate(self, client=None, books=None, pay_date="2026-01-25"):
        books = books or self.books
        return (client or self.bello).post(
            f"/v1/finance/payroll-runs/generate/?entity={books.code}",
            {"pay_date": pay_date, "period_label": "January 2026"}, format="json",
        )

    def act(self, run, action, body=None, client=None, books=None):
        books = books or self.books
        return (client or self.bello).post(
            f"/v1/finance/payroll-runs/{run.pk}/{action}/?entity={books.code}",
            body or {}, format="json",
        )

    def posted_run(self):
        response = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(pk=response.data["data"]["id"])
        posted = self.act(run, "post")
        self.assertEqual(posted.status_code, 200, posted.data)
        run.refresh_from_db()
        return run


class OneJournalPerBranchTests(_SplitFixture):

    @patch("core.person_exit.exited_states")
    def test_payroll_line_marks_a_departed_employee(self, exited_states):
        person = self.user_for(self.tenant, "departed-payroll@corona.test")
        exited_states.side_effect = lambda _tenant, ids: {
            user_id: user_id == person.pk for user_id in ids
        }
        self.salary(
            self.books, "Departed Payroll", self.ikeja, gross=10_000,
            employee=person,
        )

        response = self.generate()

        self.assertEqual(response.status_code, 201, response.data)
        line = next(
            row for row in response.data["data"]["lines"]
            if row["employee_name"] == "Departed Payroll"
        )
        self.assertTrue(line["employee_is_exited"])
        anonymous = next(
            row for row in response.data["data"]["lines"]
            if row["employee_name"] == "Ada Obi"
        )
        self.assertNotIn("employee_is_exited", anonymous)

    def test_a_central_run_posts_one_journal_per_branch(self):
        run = self.posted_run()

        self.assertIsNone(run.branch_id)
        self.assertIsNone(run.journal_id)
        shares = {s.branch_id: s for s in run.branch_shares.all()}
        self.assertEqual(set(shares), {self.ikeja.pk, self.lekki.pk})
        ikeja, lekki = shares[self.ikeja.pk], shares[self.lekki.pk]
        self.assertEqual((ikeja.gross_total, ikeja.net_total), (50_000, 45_000))
        self.assertEqual((lekki.gross_total, lekki.net_total), (80_000, 72_000))
        for share in (ikeja, lekki):
            self.assertEqual(share.journal.branch_id, share.branch_id)
            self.assertEqual(share.journal.status, DocumentStatus.POSTED)
            debit, credit = share.journal.totals()
            self.assertEqual((debit, credit), (share.gross_total, share.gross_total))

    def test_each_line_records_the_branch_it_was_booked_to(self):
        run = self.posted_run()
        self.assertEqual(
            dict(run.lines.values_list("employee_name", "branch_id")),
            {"Ada Obi": self.ikeja.pk, "Bola Lawal": self.lekki.pk},
        )

    def test_the_run_reports_its_branch_shares(self):
        run = self.posted_run()
        data = self.bello.get(
            f"/v1/finance/payroll-runs/{run.pk}/?entity={self.books.code}").data["data"]

        shares = {s["branch_name"]: s for s in data["branch_shares"]}
        self.assertEqual(set(shares), {"Ikeja Branch", "Lekki Branch"})
        self.assertEqual(shares["Ikeja Branch"]["net_total"], 45_000)
        self.assertEqual(shares["Ikeja Branch"]["status"], PayrollRunStatus.POSTED)

    def test_an_employee_with_no_branch_blocks_posting_by_name(self):
        self.salary(self.books, "Okon Udo", None, gross=30_000)
        run = PayrollRun.objects.get(pk=self.generate().data["data"]["id"])

        response = self.act(run, "post")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Okon Udo", str(response.data))
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.DRAFT)
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

    def test_a_hand_typed_line_takes_its_employees_roster_branch(self):
        from vs_finance.payroll import post_payroll

        person = self.user_for(self.tenant, "chidi@corona.test")
        self.salary(self.books, "Chidi Eze", self.lekki, gross=10_000, employee=person)
        run = PayrollRun.objects.create(entity=self.books, pay_date=JAN_25)
        PayrollLine.objects.create(run=run, line_no=1, employee=person, employee_name="Chidi Eze",
                                   gross_amount=10_000)
        PayrollLine.objects.create(run=run, line_no=2, employee_name="Ada Obi", branch=self.ikeja,
                                   gross_amount=20_000)

        post_payroll(run)

        self.assertEqual(
            {s.branch_id: s.gross_total for s in run.branch_shares.all()},
            {self.lekki.pk: 10_000, self.ikeja.pk: 20_000},
        )

    def test_a_run_whose_staff_sit_in_one_branch_posts_one_journal(self):
        self.bola.delete()
        run = self.posted_run()

        self.assertFalse(run.branch_shares.exists())
        self.assertEqual(run.journal.branch_id, self.ikeja.pk)

    def test_a_one_branch_school_posts_one_journal_as_before(self):
        self.salary(self.solo_books, "Solo Staff", None, gross=40_000)
        harbour = self.client_for(self.solo_tenant, "split-solo@harbour.test", branch=self.solo_main)

        run = PayrollRun.objects.get(pk=self.generate(harbour, self.solo_books).data["data"]["id"])
        response = self.act(run, "post", client=harbour, books=self.solo_books)

        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertFalse(run.branch_shares.exists())
        self.assertEqual(run.journal.branch_id, self.solo_main.pk)
        legacy = self.bank(self.solo_books, "Harbour Cash", None, "73")
        paid = self.act(run, "pay", {"bank_account": legacy.pk}, client=harbour, books=self.solo_books)
        self.assertEqual(paid.status_code, 200, paid.data)


class PayingEachBranchsShareTests(_SplitFixture):

    def test_each_account_pays_its_own_branchs_share(self):
        run = self.posted_run()

        first = self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})
        self.assertEqual(first.status_code, 200, first.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)
        ikeja = run.branch_shares.get(branch=self.ikeja)
        self.assertEqual(ikeja.status, PayrollRunStatus.PAID)
        self.assertEqual(ikeja.disbursement_journal.branch_id, self.ikeja.pk)
        self.assertEqual(ikeja.disbursement_journal.totals(), (45_000, 45_000))

        second = self.act(run, "pay", {"bank_accounts": [self.lekki_bank.pk]})
        self.assertEqual(second.status_code, 200, second.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        lekki = run.branch_shares.get(branch=self.lekki)
        self.assertEqual(lekki.bank_account_id, self.lekki_bank.pk)

    def test_both_shares_can_be_paid_in_one_call(self):
        run = self.posted_run()
        response = self.act(run, "pay", {"bank_accounts": [self.ikeja_bank.pk, self.lekki_bank.pk]})

        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)

    def test_an_account_of_a_branch_the_run_does_not_pay_is_refused(self):
        run = self.posted_run()
        yaba = self.bank(self.books, "Yaba Operations", self.yaba, "74")

        response = self.act(run, "pay", {"bank_account": yaba.pk})

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("pays no staff there", str(response.data))

    def test_a_share_is_paid_once(self):
        run = self.posted_run()
        self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})

        again = self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})

        self.assertEqual(again.status_code, 422, again.data)
        self.assertIn("already paid", str(again.data))

    def test_naming_no_account_says_each_branch_pays_its_own(self):
        run = self.posted_run()
        response = self.act(run, "pay", {})

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("each branch's bank account", str(response.data))

    def test_a_central_run_names_no_bank_account_when_raised(self):
        response = self.bello.post(
            f"/v1/finance/payroll-runs/?entity={self.books.code}",
            {"pay_date": "2026-01-25", "bank_account": self.ikeja_bank.pk,
             "lines": [{"employee_name": "Ada Obi", "gross_amount": 500_00, "branch": self.ikeja.pk}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("each branch's own account", str(response.data))


class AwaitingPaymentTests(_SplitFixture):
    """The summary's "awaiting payment" is the net pay no bank has paid yet.

    Once Ikeja's share of January is paid, only Lekki's 72,000 is still owed,
    though the run as a whole still reads POSTED until Lekki's is paid too.
    """

    def summary(self):
        response = self.bello.get(f"/v1/finance/payroll-runs/summary/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_a_posted_run_awaits_its_whole_net_pay(self):
        self.posted_run()
        self.assertEqual(self.summary()["to_pay"], 117_000)

    def test_a_paid_branch_share_no_longer_awaits_payment(self):
        run = self.posted_run()
        self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})

        self.assertEqual(self.summary()["to_pay"], 72_000)

    def test_a_run_posted_as_one_journal_awaits_its_net_until_paid(self):
        self.bola.delete()
        run = self.posted_run()
        self.assertEqual(self.summary()["to_pay"], 45_000)

        self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})
        self.assertEqual(self.summary()["to_pay"], 0)


class CancellingPerJournalTests(_SplitFixture):

    def test_cancelling_reverses_each_branchs_journal_on_its_own(self):
        run = self.posted_run()
        journals = {s.branch_id: s.journal_id for s in run.branch_shares.all()}

        response = self.act(run, "cancel")

        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.CANCELLED)
        for branch_id, journal_id in journals.items():
            original = JournalEntry.objects.get(pk=journal_id)
            self.assertEqual(original.status, DocumentStatus.REVERSED)
            self.assertEqual(original.reversed_by.branch_id, branch_id)
        self.assertTrue(all(s.status == PayrollRunStatus.CANCELLED for s in run.branch_shares.all()))

    def test_a_run_with_a_paid_share_cannot_be_voided(self):
        run = self.posted_run()
        self.act(run, "pay", {"bank_account": self.ikeja_bank.pk})

        response = self.act(run, "cancel")

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Ikeja Branch already paid", str(response.data))

    def test_a_branch_journal_cannot_be_reversed_on_its_own(self):
        from vs_finance.posting import reverse_journal

        run = self.posted_run()
        share = run.branch_shares.first()
        with self.assertRaises(PostingError):
            reverse_journal(share.journal)


class WhoRaisesACentralRunTests(_SplitFixture):

    def test_a_branch_officer_cannot_raise_a_run_for_all_staff(self):
        adeyemi = self.client_for(self.tenant, "split-ikeja@corona.test", branch=self.ikeja)

        response = self.generate(adeyemi)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(PayrollRun.objects.filter(entity=self.books).exists())

    def test_the_service_refuses_an_unplaced_line_directly(self):
        from vs_finance.payroll import post_payroll

        run = PayrollRun.objects.create(entity=self.books, pay_date=JAN_25)
        PayrollLine.objects.create(run=run, line_no=1, employee_name="Nobody", gross_amount=1_000)

        with self.assertRaises(PayrollBranchUnassignedError):
            post_payroll(run)
        self.assertFalse(PayrollRunBranch.objects.filter(run=run).exists())
