"""A document is checked against its own branch's month, not the school's.

Corona closes its months branch by branch. Ikeja has closed January; Lekki and
Yaba are still finishing it, so the school's January is open. Mrs Okafor's Ikeja
journal dated January was returned to her by Mr Adeyemi. When she resumes it,
it must be refused there and then, naming Ikeja and January, rather than going
back to Mr Adeyemi and failing when he approves it. A Lekki journal dated the
same day resumes as usual. The same holds when a document is first sent, and
for every finance document that posts on approval, including the credit note
and bank transaction, which before checked no month at all, or only the
school's.

At a school with one branch the refusal does not name the branch: the
dimension recedes.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.services.actions import record_action
from vs_workflow.services.submission import submit_for_approval

from .constants import DocumentStatus, PeriodStatus
from .exceptions import PeriodClosedError
from .models import (
    Account, BankTransaction, BranchFiscalPeriod, CreditNote, JournalEntry, JournalLine,
)
from .posting import _period_accepts_posting, ensure_period_open, resolve_period
from .tests_approval_resubmit import KEYS, _ResubmitFixture
from .tests_inter_branch import JAN_15


class _BranchMonthFixture(_ResubmitFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_okafor = cls.bursar(cls.lekki, keys=KEYS)
        cls.january = resolve_period(cls.books, JAN_15)

    def close_january_at(self, branch, status=PeriodStatus.CLOSED):
        BranchFiscalPeriod.objects.update_or_create(
            period=self.january, branch=branch, defaults={"status": status})

    def draft_journal(self, branch):
        entry = JournalEntry.objects.create(
            entity=self.books, branch=branch, date=JAN_15, period=self.january,
            narration="Accrued audit fee",
        )
        for line_no, (code, debit, credit) in enumerate(
                (("5300", 10_000, 0), ("2400", 0, 10_000)), start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        return entry

    def sent_and_returned(self, document, requester):
        instance = submit_for_approval(document, requested_by=requester)
        record_action(instance.id, self.signers[self.tenant.pk], WorkflowStageAction.RETURNED,
                      comment="Check the narration")
        return instance

    def resume(self, instance, requester):
        return TenantAPIClient(user=requester).post(
            f"/v1/workflow/instances/{instance.id}/resubmit/", {}, format="json")

    def assert_refused_for_ikeja(self, response):
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "PERIOD_CLOSED")
        message = response.data["message"]
        self.assertIn("Ikeja Branch has closed January 2026", message)
        self.assertNotIn("[", message)
        self.assertNotIn("CLOSED", message)
        self.assertEqual(response.data["error"]["detail"]["status"], "CLOSED")


class ResumingChecksTheBranchMonthTests(_BranchMonthFixture):

    def test_an_ikeja_journal_is_refused_when_ikeja_has_closed_its_month(self):
        entry = self.draft_journal(self.ikeja)
        instance = self.sent_and_returned(entry, self.okafor)
        self.close_january_at(self.ikeja)

        response = self.resume(instance, self.okafor)

        self.assert_refused_for_ikeja(response)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.RETURNED)
        self.assertEqual(JournalEntry.objects.get(pk=entry.pk).status, DocumentStatus.DRAFT)

    def test_a_lekki_journal_of_the_same_month_resumes(self):
        self.close_january_at(self.ikeja)
        entry = self.draft_journal(self.lekki)
        instance = self.sent_and_returned(entry, self.lekki_okafor)

        response = self.resume(instance, self.lekki_okafor)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(JournalEntry.objects.get(pk=entry.pk).status,
                         DocumentStatus.PENDING_APPROVAL)

    def test_sending_an_ikeja_journal_is_refused_too(self):
        self.close_january_at(self.ikeja)
        entry = self.draft_journal(self.ikeja)

        response = TenantAPIClient(user=self.okafor).post(
            self.url(f"journals/{entry.pk}/submit/"), {}, format="json")

        self.assert_refused_for_ikeja(response)
        self.assertEqual(JournalEntry.objects.get(pk=entry.pk).status, DocumentStatus.DRAFT)

    def test_a_credit_note_is_refused_when_its_branch_has_closed_the_month(self):
        made = TenantAPIClient(user=self.okafor).post(self.url("credit-notes/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "CREDIT",
            "note_date": "2026-01-16", "reason": "Overcharged",
            "lines": [{"description": "Bus fee", "revenue_account": "4100",
                       "quantity": 1, "unit_price": 50_000_00}],
        }, format="json")
        note = CreditNote.objects.get(pk=made.data["data"]["id"])
        instance = self.sent_and_returned(note, self.okafor)
        self.close_january_at(self.ikeja)

        self.assert_refused_for_ikeja(self.resume(instance, self.okafor))
        self.assertEqual(CreditNote.objects.get(pk=note.pk).status, DocumentStatus.DRAFT)

    def test_a_bank_transaction_is_refused_when_its_branch_has_closed_the_month(self):
        made = TenantAPIClient(user=self.okafor).post(self.url("bank-transactions/"), {
            "bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": 5_000_000,
            "counter_account": "3100", "transaction_date": "2026-01-15",
            "narration": "Owner capital",
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        txn = BankTransaction.objects.get(pk=made.data["data"]["id"])
        instance = self.instance_of(BankTransaction, txn.pk)  # Sent when it was raised.
        record_action(instance.id, self.signers[self.tenant.pk], WorkflowStageAction.RETURNED,
                      comment="Check the narration")
        self.close_january_at(self.ikeja)

        self.assert_refused_for_ikeja(self.resume(instance, self.okafor))
        self.assertEqual(BankTransaction.objects.get(pk=txn.pk).status, DocumentStatus.DRAFT)


class TheGuardNeedsTheBranchTests(_BranchMonthFixture):

    def test_leaving_out_the_branch_is_a_programming_error(self):
        with self.assertRaises(TypeError):
            ensure_period_open(self.january)
        with self.assertRaises(TypeError):
            _period_accepts_posting(self.january)

    def test_the_landing_test_reads_each_branch_own_month(self):
        self.close_january_at(self.ikeja)

        self.assertFalse(_period_accepts_posting(self.january, branch=self.ikeja))
        self.assertTrue(_period_accepts_posting(self.january, branch=self.lekki))
        self.assertTrue(_period_accepts_posting(self.january, branch=None))

    def test_a_soft_closed_branch_month_names_its_state_in_words(self):
        self.close_january_at(self.ikeja, PeriodStatus.SOFT_CLOSED)

        with self.assertRaises(PeriodClosedError) as caught:
            ensure_period_open(self.january, branch=self.ikeja)

        self.assertIn("Ikeja Branch has soft-closed January 2026", caught.exception.message)
        self.assertEqual(caught.exception.extra["status"], "SOFT_CLOSED")

    def test_a_one_branch_school_is_not_told_its_branch(self):
        january = resolve_period(self.solo_books, JAN_15)
        BranchFiscalPeriod.objects.create(
            period=january, branch=self.solo_main, status=PeriodStatus.CLOSED)

        with self.assertRaises(PeriodClosedError) as caught:
            ensure_period_open(january, branch=self.solo_main)

        message = caught.exception.message
        self.assertTrue(message.startswith("January 2026 is closed"), message)
        self.assertNotIn(self.solo_main.name, message)

    def test_a_closed_school_month_reads_as_a_month_not_a_code(self):
        from .models import FiscalPeriod

        FiscalPeriod.objects.filter(pk=self.january.pk).update(status=PeriodStatus.CLOSED)
        self.january.refresh_from_db()

        with self.assertRaises(PeriodClosedError) as caught:
            ensure_period_open(self.january, branch=None)

        self.assertEqual(
            caught.exception.message,
            "January 2026 is closed, so nothing more can be posted into it. Reopen "
            "January 2026, or use a date in an open month.",
        )
        self.assertEqual(str(self.january), self.january.name)


class PeriodClosedWordingTests(SimpleTestCase):

    def test_a_locked_year_never_suggests_reopening(self):
        error = PeriodClosedError(
            period_label="March 2026", status="LOCKED", fiscal_year_label="FY2026")
        self.assertEqual(
            error.message,
            "The FY2026 financial year is locked, so nothing more can be posted into "
            "March 2026. A locked year never reopens; use a date in an open year.",
        )
        self.assertEqual(error.extra["fiscal_year"], "FY2026")

    def test_a_branch_year_names_the_branch(self):
        error = PeriodClosedError(
            period_label="March 2026", status="CLOSED", fiscal_year_label="FY2026",
            branch_name="Lekki Branch")
        self.assertIn("Lekki Branch has closed its FY2026 financial year", error.message)
        self.assertIn("Reopen the year for Lekki Branch first", error.message)

