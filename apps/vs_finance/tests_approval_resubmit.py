"""A document returned to its requester and resumed from the approvals screen waits again.

Mr Adeyemi returns Mrs Okafor's journal, credit note and bank transaction to her
for changes. Returned, each is her draft. When she resumes the request from her
approvals screen (``POST /v1/workflow/instances/<id>/resubmit/``), the approvers
are deciding it again, so it reads PENDING_APPROVAL, as it did when she first
sent it, and nothing she could change while it was returned can be changed now:
not the bank transaction through PATCH, and none of them by sending a second
request through its own route. Resuming is the sanctioned path for a returned
document; its own ``submit/`` refuses it while the request is open.

Resuming runs the same posting guards as submitting, so a journal that could no
longer post is refused before the approvers see it again.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowInstanceStatus, WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action

from .constants import DocumentStatus
from .models import Account, BankTransaction, CreditNote, JournalEntry, JournalLine
from .posting import resolve_period
from .tests_adjustment_rework import _ReworkFixture
from .tests_inter_branch import JAN_15

KEYS = (
    "finance.creditnote.view", "finance.creditnote.create", "finance.creditnote.submit",
    "finance.journal.submit", "finance.journal.view",
    "finance.banktransaction.view", "finance.banktransaction.create",
    "finance.concession.view", "finance.concession.create", "finance.concession.submit",
)


class _ResubmitFixture(_ReworkFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okafor = cls.bursar(cls.ikeja, keys=KEYS)
        for document_type in ("finance.journal", "finance.bank_transaction"):
            template = WorkflowTemplate.objects.create(
                tenant=cls.tenant, branch=None, document_type=document_type,
                code="standard", name="Second signature")
            WorkflowStage.objects.create(
                template=template, code="signer", label="Second signature", order=10,
                approver_role_key="finance-approver")

    def setUp(self):
        self.client_ = TenantAPIClient(user=self.okafor)

    def instance_of(self, model, pk):
        return WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type=model.workflow_document_type,
        ).order_by("-created_at").first()

    def return_then_resume(self, model, pk):
        """Mr Adeyemi returns it; Mrs Okafor resumes it from her approvals screen."""
        instance = self.instance_of(model, pk)
        record_action(instance.id, self.signers[self.tenant.pk], WorkflowStageAction.RETURNED,
                      comment="Check the narration")
        self.assertEqual(model.objects.get(pk=pk).status, DocumentStatus.DRAFT)
        return self.client_.post(f"/v1/workflow/instances/{instance.id}/resubmit/", {},
                                 format="json")

    def resumed(self, model, pk):
        response = self.return_then_resume(model, pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.instance_of(model, pk).status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(model.objects.get(pk=pk).status, DocumentStatus.PENDING_APPROVAL)

    def journal(self):
        entry = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=JAN_15,
            period=resolve_period(self.books, JAN_15), narration="Accrued audit fee",
        )
        for line_no, (code, debit, credit) in enumerate(
                (("5300", 10_000, 0), ("2400", 0, 10_000)), start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        sent = self.client_.post(self.url(f"journals/{entry.pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        return entry.pk


class ResumedFromApprovalsTests(_ResubmitFixture):

    def test_a_resumed_journal_waits_for_approval_and_is_not_sent_twice(self):
        pk = self.journal()

        self.resumed(JournalEntry, pk)

        again = self.client_.post(self.url(f"journals/{pk}/submit/"), {}, format="json")
        self.assertNotEqual(again.status_code, 200, again.data)
        self.assertEqual(WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.journal").count(), 1)

    def test_a_resumed_credit_note_waits_for_approval(self):
        made = self.client_.post(self.url("credit-notes/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "CREDIT",
            "note_date": "2026-01-16", "reason": "Overcharged",
            "lines": [{"description": "Bus fee", "revenue_account": "4100",
                       "quantity": 1, "unit_price": 50_000_00}],
        }, format="json")
        pk = made.data["data"]["id"]
        self.client_.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")

        self.resumed(CreditNote, pk)

        again = self.client_.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")
        self.assertNotEqual(again.status_code, 200, again.data)

    def test_a_resumed_bank_transaction_waits_and_cannot_be_corrected(self):
        made = self.client_.post(self.url("bank-transactions/"), {
            "bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": 5_000_000,
            "counter_account": "3100", "transaction_date": "2026-01-15",
            "narration": "Owner capital",
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]

        self.resumed(BankTransaction, pk)

        edited = self.client_.patch(self.url(f"bank-transactions/{pk}/"),
                                    {"amount": 1}, format="json")
        self.assertEqual(edited.status_code, 422, edited.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).amount, 5_000_000)

    def test_resuming_a_journal_that_can_no_longer_post_is_refused(self):
        from .models import FiscalPeriod

        pk = self.journal()
        instance = self.instance_of(JournalEntry, pk)
        FiscalPeriod.objects.filter(pk=JournalEntry.objects.get(pk=pk).period_id).update(
            status="CLOSED")

        response = self.return_then_resume(JournalEntry, pk)

        self.assertNotEqual(response.status_code, 200, response.data)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.RETURNED)
        self.assertEqual(JournalEntry.objects.get(pk=pk).status, DocumentStatus.DRAFT)

    def test_only_the_requester_resumes_it(self):
        pk = self.journal()
        instance = self.instance_of(JournalEntry, pk)
        record_action(instance.id, self.signers[self.tenant.pk], WorkflowStageAction.RETURNED,
                      comment="Check it")

        response = TenantAPIClient(user=self.lekki_bursar).post(
            f"/v1/workflow/instances/{instance.id}/resubmit/", {}, format="json")

        self.assertNotEqual(response.status_code, 200, response.data)
        self.assertEqual(JournalEntry.objects.get(pk=pk).status, DocumentStatus.DRAFT)
