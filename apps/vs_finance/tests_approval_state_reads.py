"""Every approval-gated finance document says where it stands with its approval route.

Mrs Okafor's credit notes at Ikeja: one never sent (NOT_SUBMITTED), one waiting
on Mr Adeyemi (PENDING), one he sent back to her (a draft that still reads
PENDING, because its request is open and is resumed from the approvals screen),
one she resumed (PENDING_APPROVAL and PENDING), and one he rejected (REJECTED).
FinPro hides Submit, Send and Edit on a draft that reads PENDING, so the sent
back case is the one that matters most.

The state is read once per page, so a page of fifty costs what a page of five
costs.
"""
from __future__ import annotations

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from vs_workflow.constants import WorkflowStageAction
from vs_workflow.services.actions import record_action

from .models import CreditNote, JournalEntry
from .tests_approval_resubmit import _ResubmitFixture


class EveryKindCarriesTheStateTests(SimpleTestCase):

    def test_each_read_shape_names_approval_state(self):
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
        from .views_ops.interbranch import InterBranchTransferSerializer

        for serializer in (
            CreditNoteSerializer, ConcessionSerializer, RefundSerializer,
            WriteOffRequestSerializer, CustomerCreditTransferSerializer,
            DoubtfulDebtProvisionSerializer, JournalEntryListSerializer,
            JournalEntryDetailSerializer, ExpenseClaimSerializer, InterBranchTransferSerializer,
        ):
            with self.subTest(serializer=serializer.__name__):
                self.assertIn("approval_state", serializer.Meta.fields)


class ApprovalStateReadTests(_ResubmitFixture):

    def note(self, *, submit=True):
        made = self.client_.post(self.url("credit-notes/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "CREDIT",
            "note_date": "2026-01-16", "reason": "Overcharged",
            "lines": [{"description": "Bus fee", "revenue_account": "4100",
                       "quantity": 1, "unit_price": 1_000_00}],
        }, format="json")
        self.assertEqual(made.status_code, 201, made.data)
        pk = made.data["data"]["id"]
        if submit:
            sent = self.client_.post(self.url(f"credit-notes/{pk}/submit/"), {}, format="json")
            self.assertEqual(sent.status_code, 200, sent.data)
        return pk

    def decide(self, model, pk, action):
        record_action(self.instance_of(model, pk).id, self.signers[self.tenant.pk], action,
                      comment="Check it")

    def states(self, path):
        response = self.client_.get(self.url(path))
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"]: (row["status"], row["approval_state"]) for row in response.data["data"]}

    def test_each_state_of_a_credit_note_reads_through(self):
        never = self.note(submit=False)
        waiting = self.note()
        sent_back = self.note()
        self.decide(CreditNote, sent_back, WorkflowStageAction.RETURNED)
        resumed = self.note()
        self.decide(CreditNote, resumed, WorkflowStageAction.RETURNED)
        self.client_.post(f"/v1/workflow/instances/{self.instance_of(CreditNote, resumed).id}"
                          f"/resubmit/", {}, format="json")
        rejected = self.note()
        self.decide(CreditNote, rejected, WorkflowStageAction.REJECTED)

        listed = self.states("credit-notes/")

        self.assertEqual(listed[never], ("DRAFT", "NOT_SUBMITTED"))
        self.assertEqual(listed[waiting], ("PENDING_APPROVAL", "PENDING"))
        self.assertEqual(listed[sent_back], ("DRAFT", "PENDING"))
        self.assertEqual(listed[resumed], ("PENDING_APPROVAL", "PENDING"))
        self.assertEqual(listed[rejected], ("DRAFT", "REJECTED"))
        detail = self.client_.get(self.url(f"credit-notes/{sent_back}/")).data["data"]
        self.assertEqual(detail["approval_state"], "PENDING")

    def test_a_sent_back_journal_reads_pending_in_the_ledger_list_and_detail(self):
        pk = self.journal()
        self.decide(JournalEntry, pk, WorkflowStageAction.RETURNED)

        listed = self.states("journals/")
        detail = self.client_.get(self.url(f"journals/{pk}/")).data["data"]

        self.assertEqual(listed[pk], ("DRAFT", "PENDING"))
        self.assertEqual(detail["approval_state"], "PENDING")

    def test_a_withdrawn_concession_reads_not_submitted(self):
        from vs_workflow.services.actions import withdraw

        from .models import Concession

        made = self.client_.post(self.url("concessions/"), {
            "customer": "TUNDE", "invoice": self.bill.pk, "kind": "SCHOLARSHIP",
            "concession_date": "2026-01-16", "amount": 30_000_00, "reason": "Bursary",
        }, format="json")
        pk = made.data["data"]["id"]
        sent = self.client_.post(self.url(f"concessions/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.data["data"]["approval_state"], "PENDING")
        withdraw(self.instance_of(Concession, pk).id, self.okafor)

        self.assertEqual(self.states("concessions/")[pk], ("DRAFT", "NOT_SUBMITTED"))

    def test_a_page_costs_the_same_queries_however_many_documents(self):
        for _ in range(2):
            self.note()
        with CaptureQueriesContext(connection) as few:
            self.states("credit-notes/")
            self.states("journals/")
        for _ in range(5):
            self.note()
            self.journal()
        with CaptureQueriesContext(connection) as many:
            self.states("credit-notes/")
            self.states("journals/")

        self.assertEqual(len(many), len(few))
