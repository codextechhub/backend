"""A finance document an approver returns is corrected by whoever sent it, then resumed.

Mrs Okafor, Ikeja's bursar, sends Mr Adeyemi a N10,000 accrual journal, a N50,000
credit note on Tunde's bill, a N50,000 bank receipt of owner capital and a N30,000
bursary. Mr Adeyemi returns each to her with a comment instead of rejecting it:
the accrual is N8,000, the credit note covers the bus only, the owner put in
N40,000, the bursary is N25,000. Each is her draft again and reads
``approval_returned: true``. She corrects it through its own edit route and
resumes the request from her approvals screen
(``POST /v1/workflow/instances/<id>/resubmit/``): Mr Adeyemi sees the corrected
document, and what he approves is what posts.

Only Mrs Okafor corrects what was returned to her. Mr Bassey, her Ikeja colleague
holding the same keys, is refused (403), Lekki's bursar does not reach it (404),
and nobody edits it while Mr Adeyemi holds it or once it has posted (422).
Single Site, with one branch, corrects and resumes its direct entry the same way.
"""
from __future__ import annotations

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.models import WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action

from .constants import DocumentStatus, FinanceAuditAction
from .models import (
    BankTransaction,
    Concession,
    CreditNote,
    ExpenseClaim,
    FinanceAuditLog,
    JournalEntry,
)
from .tests_approval_resubmit import KEYS, _ResubmitFixture

ENTRY_KEYS = KEYS + (
    "finance.directentry.post", "finance.directentry.view",
    "finance.expenseclaim.view", "finance.expenseclaim.create", "finance.expenseclaim.submit",
)


class _ReturnedFixture(_ResubmitFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okafor = cls.bursar(cls.ikeja, keys=ENTRY_KEYS)
        cls.bassey = cls.bursar(cls.ikeja, keys=ENTRY_KEYS)
        cls.lekki_bursar = cls.bursar(cls.lekki, keys=ENTRY_KEYS)
        cls.hq = cls.bursar(None, keys=ENTRY_KEYS)
        cls.solo_bursar = cls.bursar(None, tenant=cls.solo_tenant, keys=ENTRY_KEYS)
        cls.viewer = cls.bursar(cls.ikeja, keys=(
            "finance.journal.view", "finance.creditnote.view", "finance.banktransaction.view",
            "finance.concession.view", "finance.expenseclaim.view"))
        routes = ((cls.solo_tenant, "finance.journal"), (cls.tenant, "finance.expense_claim"))
        for tenant, document_type in routes:
            template = WorkflowTemplate.objects.create(
                tenant=tenant, branch=None, document_type=document_type,
                code="standard", name="Second signature")
            WorkflowStage.objects.create(
                template=template, code="signer", label="Second signature", order=10,
                approver_role_key="finance-approver")

    def setUp(self):
        self.client_ = TenantAPIClient(user=self.okafor)

    def as_(self, user):
        return TenantAPIClient(user=user)

    def decide(self, model, pk, action):
        instance = self.instance_of(model, pk)
        record_action(instance.id, self.signers[instance.tenant_id], action, comment="Check it")
        return instance

    def returned(self, model, pk):
        return self.decide(model, pk, WorkflowStageAction.RETURNED)

    def resume(self, user, instance):
        response = self.as_(user).post(
            f"/v1/workflow/instances/{instance.id}/resubmit/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        return instance

    def direct_entry(self, user, *, amount=10_000_00, books=None):
        response = self.as_(user).post(self.url("direct-entries/", books), {
            "date": "2026-01-15", "narration": "Accrued audit fee",
            "lines": [{"account": "5300", "debit": amount},
                      {"account": "2400", "credit": amount}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        return response.data["data"]["id"]

    def entry_body(self, amount, narration="Accrued audit fee, January"):
        return {"narration": narration,
                "lines": [{"account": "5300", "debit": amount},
                          {"account": "2400", "credit": amount}]}


class ReturnedJournalTests(_ReturnedFixture):

    def test_the_sender_corrects_a_returned_direct_entry_resumes_it_and_it_posts_corrected(self):
        pk = self.direct_entry(self.okafor)
        instance = self.returned(JournalEntry, pk)
        url = self.url(f"journals/{pk}/")
        before = self.client_.get(url).data["data"]
        self.assertEqual((before["status"], before["approval_state"], before["approval_returned"]),
                         ("DRAFT", "PENDING", True))
        listed = {row["id"]: row for row in self.client_.get(self.url("journals/")).data["data"]}
        self.assertTrue(listed[pk]["approval_returned"])

        edited = self.client_.patch(url, self.entry_body(8_000_00), format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        row = edited.data["data"]
        self.assertEqual((row["status"], row["approval_state"], row["approval_returned"]),
                         ("DRAFT", "PENDING", True))
        self.assertEqual(sorted((line["debit"], line["credit"]) for line in row["lines"]),
                         [(0, 8_000_00), (8_000_00, 0)])
        self.resume(self.okafor, instance)
        self.assertIn("8,000.00", str(instance.document_summary))
        after = self.client_.get(url).data["data"]
        self.assertEqual((after["status"], after["approval_returned"]),
                         ("PENDING_APPROVAL", False))
        self.decide(JournalEntry, pk, WorkflowStageAction.APPROVED)
        entry = JournalEntry.objects.get(pk=pk)
        self.assertEqual((entry.status, entry.total_debit_kobo, entry.narration),
                         (DocumentStatus.POSTED, 8_000_00, "Accrued audit fee, January"))
        audit = FinanceAuditLog.objects.get(action=FinanceAuditAction.JOURNAL_EDITED,
                                            target_id=str(pk))
        self.assertEqual(audit.branch_id, self.ikeja.pk)

    def test_only_the_sender_corrects_it_within_reach_and_holding_the_key(self):
        pk = self.direct_entry(self.okafor)
        self.returned(JournalEntry, pk)
        url = self.url(f"journals/{pk}/")
        body = self.entry_body(1_00)

        colleague = self.as_(self.bassey).patch(url, body, format="json")
        head_office = self.as_(self.hq).patch(url, body, format="json")
        lekki = self.as_(self.lekki_bursar).patch(url, body, format="json")
        viewer = self.as_(self.viewer).patch(url, body, format="json")
        rival = self.as_(self.rival).patch(
            self.url(f"journals/{pk}/", self.rival_books), body, format="json")

        self.assertEqual(colleague.status_code, 403, colleague.data)
        self.assertEqual(head_office.status_code, 403, head_office.data)
        self.assertEqual(lekki.status_code, 404, lekki.data)
        self.assertEqual(viewer.status_code, 403, viewer.data)
        self.assertIn(rival.status_code, (403, 404), rival.data)
        self.assertEqual(JournalEntry.objects.get(pk=pk).total_debit_kobo, 10_000_00)

    def test_nobody_edits_it_while_its_approver_holds_it_or_once_it_has_posted(self):
        pk = self.direct_entry(self.okafor)
        url = self.url(f"journals/{pk}/")

        waiting = self.client_.patch(url, self.entry_body(1_00), format="json")
        instance = self.returned(JournalEntry, pk)
        self.resume(self.okafor, instance)
        resumed = self.client_.patch(url, self.entry_body(1_00), format="json")
        self.decide(JournalEntry, pk, WorkflowStageAction.APPROVED)
        posted = self.client_.patch(url, self.entry_body(1_00), format="json")

        for response in (waiting, resumed, posted):
            self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(JournalEntry.objects.get(pk=pk).total_debit_kobo, 10_000_00)

    def test_a_rejected_entry_is_corrected_and_sent_again_through_its_route(self):
        pk = self.direct_entry(self.okafor)
        self.decide(JournalEntry, pk, WorkflowStageAction.REJECTED)

        edited = self.client_.patch(self.url(f"journals/{pk}/"), self.entry_body(9_000_00),
                                    format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual((edited.data["data"]["approval_state"],
                          edited.data["data"]["approval_returned"]), ("REJECTED", False))
        sent = self.client_.post(self.url(f"journals/{pk}/submit/"), {}, format="json")

        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)

    def test_a_correction_is_checked_as_a_new_entry_is_and_keeps_its_branch(self):
        pk = self.direct_entry(self.okafor)
        self.returned(JournalEntry, pk)
        url = self.url(f"journals/{pk}/")

        unbalanced = self.client_.patch(url, {"lines": [
            {"account": "5300", "debit": 8_000_00}, {"account": "2400", "credit": 7_000_00}]},
            format="json")
        moved = self.client_.patch(url, {"branch": self.lekki.pk}, format="json")
        bank_line = self.client_.patch(url, {"lines": [
            {"account": "5300", "debit": 8_000_00},
            {"account": self.ikeja_bank.gl_account.code, "credit": 8_000_00}]}, format="json")

        self.assertEqual(unbalanced.status_code, 400, unbalanced.data)
        self.assertEqual(moved.status_code, 400, moved.data)
        self.assertGreaterEqual(bank_line.status_code, 400, bank_line.data)
        self.assertLess(bank_line.status_code, 500, bank_line.data)
        entry = JournalEntry.objects.get(pk=pk)
        self.assertEqual((entry.branch_id, entry.total_debit_kobo), (self.ikeja.pk, 10_000_00))

    def test_a_journal_another_document_raised_is_corrected_through_that_document(self):
        from .models import Account, JournalLine
        from .posting import resolve_period
        from .tests_inter_branch import JAN_15

        entry = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=JAN_15, source="BANK",
            period=resolve_period(self.books, JAN_15), narration="Bank charges",
        )
        for line_no, (code, debit, credit) in enumerate(
                (("5300", 1_000, 0), ("2400", 0, 1_000)), start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no)

        response = self.client_.patch(self.url(f"journals/{entry.pk}/"), self.entry_body(5_00),
                                      format="json")

        self.assertEqual(response.status_code, 422, response.data)

    def test_a_school_with_one_branch_corrects_and_resumes_its_entry(self):
        solo = self.as_(self.solo_bursar)
        pk = self.direct_entry(self.solo_bursar, books=self.solo_books)
        instance = self.returned(JournalEntry, pk)

        edited = solo.patch(self.url(f"journals/{pk}/", self.solo_books),
                            self.entry_body(6_000_00), format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.resume(self.solo_bursar, instance)
        entry = JournalEntry.objects.get(pk=pk)
        self.assertEqual((entry.branch_id, entry.status, entry.total_debit_kobo),
                         (self.solo_main.pk, DocumentStatus.PENDING_APPROVAL, 6_000_00))


class ReturnedCreditNoteTests(_ReturnedFixture):

    def raise_note(self, user=None):
        made = self.as_(user or self.okafor).post(self.url("credit-notes/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "CREDIT",
            "note_date": "2026-01-16", "reason": "Overcharged",
            "lines": [{"description": "Bus and lunch", "revenue_account": "4100",
                       "quantity": 1, "unit_price": 50_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        sent = self.as_(user or self.okafor).post(self.url(f"credit-notes/{pk}/submit/"), {},
                                                  format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        return pk

    def note_body(self):
        return {"reason": "Overcharged for the bus",
                "lines": [{"description": "Bus fee", "revenue_account": "4100",
                           "quantity": 1, "unit_price": 30_000_00}]}

    def test_the_sender_corrects_a_returned_note_and_resumes_it(self):
        pk = self.raise_note()
        instance = self.returned(CreditNote, pk)
        url = self.url(f"credit-notes/{pk}/")
        self.assertTrue(self.client_.get(url).data["data"]["approval_returned"])

        edited = self.client_.patch(url, self.note_body(), format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        row = edited.data["data"]
        self.assertEqual((row["total"], row["reason"], row["status"], row["approval_returned"]),
                         (30_000_00, "Overcharged for the bus", "DRAFT", True))
        self.assertEqual([line["description"] for line in row["lines"]], ["Bus fee"])
        self.resume(self.okafor, instance)
        self.assertIn("30,000.00", str(instance.document_summary))
        self.decide(CreditNote, pk, WorkflowStageAction.APPROVED)
        note = CreditNote.objects.get(pk=pk)
        self.assertEqual((note.status, note.total), (DocumentStatus.POSTED, 30_000_00))
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CREDIT_NOTE_EDITED, target_id=str(pk)).exists())

    def test_only_the_sender_corrects_it_and_never_while_its_approver_holds_it(self):
        pk = self.raise_note()
        url = self.url(f"credit-notes/{pk}/")

        waiting = self.client_.patch(url, self.note_body(), format="json")
        self.returned(CreditNote, pk)
        colleague = self.as_(self.bassey).patch(url, self.note_body(), format="json")
        lekki = self.as_(self.lekki_bursar).patch(url, self.note_body(), format="json")
        viewer = self.as_(self.viewer).patch(url, self.note_body(), format="json")

        self.assertEqual(waiting.status_code, 422, waiting.data)
        self.assertEqual(colleague.status_code, 403, colleague.data)
        self.assertEqual(lekki.status_code, 404, lekki.data)
        self.assertEqual(viewer.status_code, 403, viewer.data)
        self.assertEqual(CreditNote.objects.get(pk=pk).total, 50_000_00)

    def test_the_customer_invoice_and_kind_are_fixed(self):
        pk = self.raise_note()
        self.returned(CreditNote, pk)
        url = self.url(f"credit-notes/{pk}/")

        for body in ({"customer": "TUNDE"}, {"invoice": self.bill.pk}, {"kind": "DEBIT"},
                     {"lines": []}):
            with self.subTest(body=body):
                response = self.client_.patch(url, body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(CreditNote.objects.get(pk=pk).total, 50_000_00)

    def test_a_rejected_note_is_corrected_and_sent_again(self):
        pk = self.raise_note()
        self.decide(CreditNote, pk, WorkflowStageAction.REJECTED)

        edited = self.client_.patch(self.url(f"credit-notes/{pk}/"), self.note_body(),
                                    format="json")
        sent = self.client_.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(CreditNote.objects.get(pk=pk).total, 30_000_00)


class ReturnedBankTransactionTests(_ReturnedFixture):

    def capital(self, user, *, amount=5_000_000):
        made = self.as_(user).post(self.url("bank-transactions/"), {
            "bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": amount,
            "counter_account": "3100", "transaction_date": "2026-01-15",
            "narration": "Owner capital",
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        return made.data["data"]["id"]

    def test_the_sender_corrects_a_returned_transaction_and_it_posts_corrected(self):
        pk = self.capital(self.okafor)
        instance = self.returned(BankTransaction, pk)
        url = self.url(f"bank-transactions/{pk}/")
        self.assertTrue(self.client_.get(url).data["data"]["approval_returned"])

        edited = self.client_.patch(url, {"amount": 4_000_000}, format="json")

        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual((edited.data["data"]["amount"], edited.data["data"]["approval_returned"]),
                         (4_000_000, True))
        self.resume(self.okafor, instance)
        self.decide(BankTransaction, pk, WorkflowStageAction.APPROVED)
        txn = BankTransaction.objects.get(pk=pk)
        self.assertEqual((txn.status, txn.amount), (DocumentStatus.POSTED, 4_000_000))

    def test_a_colleague_cannot_correct_it_and_its_own_routes_do_not_bypass_the_resume(self):
        pk = self.capital(self.okafor)
        self.returned(BankTransaction, pk)

        colleague = self.as_(self.bassey).patch(self.url(f"bank-transactions/{pk}/"),
                                                {"amount": 1}, format="json")
        submit = self.client_.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json")
        cancel = self.client_.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json")

        self.assertEqual(colleague.status_code, 403, colleague.data)
        self.assertEqual(submit.status_code, 422, submit.data)
        self.assertEqual(cancel.status_code, 422, cancel.data)
        txn = BankTransaction.objects.get(pk=pk)
        self.assertEqual((txn.status, txn.amount), (DocumentStatus.DRAFT, 5_000_000))

    def test_a_returned_transaction_keeps_its_branch(self):
        pk = self.capital(self.hq)
        self.returned(BankTransaction, pk)

        moved = self.as_(self.hq).patch(self.url(f"bank-transactions/{pk}/"),
                                        {"bank_account": self.lekki_bank.pk}, format="json")

        self.assertEqual(moved.status_code, 400, moved.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).branch_id, self.ikeja.pk)


class ReturnedConcessionTests(_ReturnedFixture):

    def test_the_sender_corrects_a_returned_bursary_and_a_colleague_cannot(self):
        made = self.client_.post(self.url("concessions/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "SCHOLARSHIP",
            "concession_date": "2026-01-16", "amount": 30_000_00, "reason": "Bursary",
        }, format="json")
        pk = made.data["data"]["id"]
        self.client_.post(self.url(f"concessions/{pk}/submit/"), {}, format="json")
        instance = self.returned(Concession, pk)
        url = self.url(f"concessions/{pk}/")

        colleague = self.as_(self.bassey).patch(url, {"amount": 1}, format="json")
        edited = self.client_.patch(url, {"amount": 25_000_00}, format="json")

        self.assertEqual(colleague.status_code, 403, colleague.data)
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertTrue(edited.data["data"]["approval_returned"])
        self.resume(self.okafor, instance)
        self.assertEqual(Concession.objects.get(pk=pk).status, DocumentStatus.PENDING_APPROVAL)
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.CONCESSION_EDITED, target_id=str(pk)).exists())


class ReturnedExpenseClaimTests(_ReturnedFixture):

    def test_only_the_sender_changes_a_returned_claims_receipts(self):
        made = self.client_.post(self.url("expense-claims/"), {
            "claimant_name": "Mrs Okafor", "claim_date": "2026-01-10", "title": "Bank visit",
            "lines": [{"description": "Taxi", "expense_account": "5300",
                       "quantity": 1, "unit_price": 5_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        line = made.data["data"]["lines"][0]["id"]
        sent = self.client_.post(self.url(f"expense-claims/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.returned(ExpenseClaim, pk)
        url = self.url(f"expense-claims/{pk}/lines/{line}/receipt/")

        colleague = self.as_(self.bassey).delete(url)
        sender = self.client_.delete(url)

        self.assertEqual(colleague.status_code, 403, colleague.data)
        self.assertEqual(sender.status_code, 200, sender.data)
        self.assertTrue(sender.data["data"]["approval_returned"])


class EveryReadShapeSaysWhetherItWasReturnedTests(SimpleTestCase):

    def test_each_read_shape_with_an_approval_state_names_approval_returned(self):
        from .serializers import (
            ConcessionSerializer,
            CreditNoteSerializer,
            CustomerCreditTransferSerializer,
            DoubtfulDebtProvisionSerializer,
            ExpenseClaimSerializer,
            JournalEntryDetailSerializer,
            JournalEntryListSerializer,
            RefundSerializer,
            WriteOffRequestSerializer,
        )
        from .views_ops.banking import BankTransactionSerializer, BankTransferSerializer
        from .views_ops.interbranch import InterBranchTransferSerializer

        for serializer in (
            CreditNoteSerializer, ConcessionSerializer, RefundSerializer,
            WriteOffRequestSerializer, CustomerCreditTransferSerializer,
            DoubtfulDebtProvisionSerializer, JournalEntryListSerializer,
            JournalEntryDetailSerializer, ExpenseClaimSerializer, InterBranchTransferSerializer,
            BankTransactionSerializer, BankTransferSerializer,
        ):
            with self.subTest(serializer=serializer.__name__):
                self.assertIn("approval_state", serializer.Meta.fields)
                self.assertIn("approval_returned", serializer.Meta.fields)


class ReturnedIsReadOncePerPageTests(_ReturnedFixture):

    def test_a_page_costs_the_same_queries_however_many_are_returned(self):
        def page():
            listed = self.client_.get(self.url("journals/"))
            self.assertEqual(listed.status_code, 200, listed.data)
            return {row["id"]: row["approval_returned"] for row in listed.data["data"]
                    if row["source"] == "MANUAL"}

        first = self.direct_entry(self.okafor)
        self.returned(JournalEntry, first)
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(page(), {first: True})
        others = [self.direct_entry(self.okafor) for _ in range(4)]
        for pk in others[:2]:
            self.returned(JournalEntry, pk)
        with CaptureQueriesContext(connection) as many:
            flags = page()

        self.assertEqual(len(many), len(few))
        self.assertEqual(flags, {first: True, others[0]: True, others[1]: True,
                                 others[2]: False, others[3]: False})
