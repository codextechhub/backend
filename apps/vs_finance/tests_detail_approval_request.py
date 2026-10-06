"""Every approval-gated finance document's detail names its latest approval request.

Mr Adeyemi returns one of each of Mrs Okafor's Ikeja documents to her: a credit
note, a concession, a refund, a write-off, a customer credit transfer, a
doubtful-debt provision, a journal, an expense claim, an inter-branch transfer,
a bank transaction and a transfer between Ikeja's own accounts. FinPro's detail
screen offers her Resume (``POST /v1/workflow/instances/<id>/resubmit/``), and
the id it posts to is the detail's ``workflow_instance_id``: the document's
latest request, the one its ``approval_returned`` describes, never an earlier
one she withdrew. Before a document is first sent it is null.

Naming the request costs a detail one query at most.
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.models import WorkflowInstance, WorkflowInstanceQuerySet, WorkflowTemplate

from .constants import CreditNoteKind, InterBranchTransferKind
from .models import (
    Account,
    BankTransaction,
    BankTransfer,
    Concession,
    CreditNote,
    CustomerCreditTransfer,
    DoubtfulDebtProvision,
    ExpenseClaim,
    ExpenseClaimLine,
    InterBranchTransfer,
    JournalEntry,
    Refund,
    WriteOffRequest,
)
from .posting import resolve_period
from .tests_inter_branch import JAN_15
from .tests_returned_correction import _ReturnedFixture

READ_KEYS = (
    "finance.creditnote.view", "finance.concession.view", "finance.refund.view",
    "finance.writeoff.view", "finance.credittransfer.view", "finance.provision.view",
    "finance.journal.view", "finance.expenseclaim.view", "finance.interbranch.view",
    "finance.banktransaction.view", "finance.banktransfer.view",
)


class _DetailFixture(_ReturnedFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reader = cls.bursar(None, keys=READ_KEYS)
        cls.grant(cls.okafor, "finance.banktransfer.view", "finance.banktransfer.create",
                  tenant=cls.tenant, role_key="detail-bank-transfers", branch=cls.ikeja)
        cls.route = WorkflowTemplate.objects.create(
            tenant=cls.tenant, branch=None, document_type="finance.any",
            code="detail-request", name="Second signature")
        eze = cls.customer(cls.books, "EZE", cls.ikeja)
        ikeja_access = cls.bank(cls.books, "1154", "Ikeja Access", cls.ikeja)
        books, ikeja = cls.books, cls.ikeja
        cls.documents = {
            "credit-notes": CreditNote.objects.create(
                entity=books, customer=cls.tunde, invoice=cls.bill, branch=ikeja,
                kind=CreditNoteKind.CREDIT, note_date=JAN_15, reason="Bus fee"),
            "concessions": Concession.objects.create(
                entity=books, customer=cls.tunde, invoice=cls.bill, branch=ikeja,
                concession_date=JAN_15, amount=25_000_00),
            "refunds": Refund.objects.create(
                entity=books, customer=cls.tunde, branch=ikeja, refund_date=JAN_15,
                amount=5_000_00),
            "write-offs": WriteOffRequest.objects.create(
                entity=books, invoice=cls.bill, branch=ikeja, amount=10_000_00),
            "credit-transfers": CustomerCreditTransfer.objects.create(
                entity=books, branch=ikeja, from_customer=cls.tunde, to_customer=eze,
                transfer_date=JAN_15, amount=10_000_00),
            "provisions": DoubtfulDebtProvision.objects.create(entity=books, as_of=JAN_15),
            "journals": JournalEntry.objects.create(
                entity=books, branch=ikeja, date=JAN_15,
                period=resolve_period(books, JAN_15), narration="Accrued audit fee"),
            "expense-claims": ExpenseClaim.objects.create(
                entity=books, branch=ikeja, claim_date=JAN_15),
            "inter-branch-transfers": InterBranchTransfer.objects.create(
                entity=books, kind=InterBranchTransferKind.CASH, branch=ikeja,
                to_branch=cls.lekki, amount=1_000_00, transfer_date=JAN_15, purpose="Diesel",
                from_bank_account=cls.ikeja_bank, to_bank_account=cls.lekki_bank),
            "bank-transactions": BankTransaction.objects.create(
                entity=books, branch=ikeja, bank_account=cls.ikeja_bank, direction="IN",
                amount=40_000_00, counter_account=Account.objects.get(entity=books, code="3100"),
                transaction_date=JAN_15, narration="Owner capital"),
            "bank-transfers": BankTransfer.objects.create(
                entity=books, branch=ikeja, from_account=cls.ikeja_bank,
                to_account=ikeja_access, amount=20_000_00, transfer_date=JAN_15,
                narration="Float"),
        }

    def setUp(self):
        self.client_ = TenantAPIClient(user=self.reader)

    def request_for(self, document, status, *, minutes_ago=0):
        """A request for ``document`` as the engine records one, sent ``minutes_ago``."""
        instance = WorkflowInstance.all_objects.create(
            tenant=self.tenant, branch=document.branch, template=self.route,
            document_content_type=ContentType.objects.get_for_model(type(document)),
            document_object_id=str(document.pk), document_type=document.workflow_document_type,
            status=status, requested_by=self.okafor)
        WorkflowInstance.all_objects.filter(pk=instance.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=minutes_ago))
        return instance

    def detail(self, path, document):
        response = self.client_.get(self.url(f"{path}/{document.pk}/"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class DetailNamesItsApprovalRequestTests(_DetailFixture):

    def test_each_kind_names_its_latest_request_once_sent_and_null_before(self):
        for path, document in self.documents.items():
            with self.subTest(kind=path):
                self.assertIsNone(self.detail(path, document)["workflow_instance_id"])
                self.request_for(document, WorkflowInstanceStatus.WITHDRAWN, minutes_ago=30)
                latest = self.request_for(document, WorkflowInstanceStatus.RETURNED)

                row = self.detail(path, document)

                self.assertEqual(row["workflow_instance_id"], latest.id)
                self.assertTrue(row["approval_returned"])

    def test_naming_the_request_costs_a_detail_one_query_at_most(self):
        for path, document in self.documents.items():
            self.request_for(document, WorkflowInstanceStatus.RETURNED)
            url = self.url(f"{path}/{document.pk}/")
            with self.subTest(kind=path):
                self.client_.get(url)
                with mock.patch.object(WorkflowInstanceQuerySet, "latest_id_for",
                                       return_value=None):
                    with CaptureQueriesContext(connection) as without:
                        self.client_.get(url)
                with CaptureQueriesContext(connection) as named:
                    self.client_.get(url)

                self.assertLessEqual(len(named) - len(without), 1)


class CorrectionNamesItsApprovalRequestTests(_DetailFixture):
    """What Mrs Okafor's correction of a returned document answers with names the request too."""

    def test_each_correction_route_answers_with_the_request_to_resume(self):
        claim = self.documents["expense-claims"]
        line = ExpenseClaimLine.objects.create(
            claim=claim, line_no=1, description="Taxi", unit_price=5_000_00,
            expense_account=Account.objects.get(entity=self.books, code="5300"))
        corrections = (
            ("patch", "journals", {"narration": "Accrued audit fee, January"}),
            ("patch", "credit-notes", {"reason": "Bus fee only"}),
            ("patch", "concessions", {"reason": "Bursary"}),
            ("patch", "bank-transactions", {"narration": "Owner capital, January"}),
            ("patch", "bank-transfers", {"narration": "Float, January"}),
            ("delete", "expense-claims", None),
        )
        sender = TenantAPIClient(user=self.okafor)
        for method, path, body in corrections:
            document = self.documents[path]
            url = self.url(f"{path}/{document.pk}/")
            if path == "expense-claims":
                url = self.url(f"{path}/{document.pk}/lines/{line.pk}/receipt/")
            with self.subTest(kind=path):
                request = self.request_for(document, WorkflowInstanceStatus.RETURNED)

                response = getattr(sender, method)(url, body, format="json")

                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["data"]["workflow_instance_id"], request.id)

    def test_the_id_a_detail_names_is_the_one_mrs_okafor_resumes(self):
        for user, books in ((self.okafor, None), (self.solo_bursar, self.solo_books)):
            with self.subTest(books=(books or self.books).code):
                pk = self.direct_entry(user, books=books)
                self.returned(JournalEntry, pk)

                row = self.as_(user).get(self.url(f"journals/{pk}/", books)).data["data"]

                self.assertTrue(row["approval_returned"])
                instance = WorkflowInstance.all_objects.get(pk=row["workflow_instance_id"])
                self.resume(user, instance)
                after = self.as_(user).get(self.url(f"journals/{pk}/", books)).data["data"]
                self.assertEqual((after["status"], after["workflow_instance_id"]),
                                 ("PENDING_APPROVAL", instance.id))
