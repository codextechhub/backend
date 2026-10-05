"""Bank transactions and transfers say whose branch they are and where approval stands.

Mrs Okafor, Ikeja's bursar, books owner capital into Ikeja's GTBank account and moves
money from Ikeja's Zenith account into it. The lists name the branch, so a
whole-school reader sees "Ikeja Branch" rather than a number, and say whether each
document went for approval and how that ended: ``NOT_SUBMITTED`` when it posted at
once, ``PENDING`` while an approver has it, ``APPROVED`` or ``REJECTED`` once decided.
"""
from __future__ import annotations

import itertools

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate

from .tests_ledger_lock import _LockFixture

_roles = itertools.count(1)
KEYS = (
    "finance.banktransaction.view", "finance.banktransaction.create",
    "finance.banktransfer.view", "finance.banktransfer.create",
)


class BankDocumentStateTests(_LockFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_zenith = cls.bank("1152", "Ikeja Zenith", cls.ikeja)

    def bursar(self, branch=None):
        user = self.user_for(self.tenant, f"states-{next(_roles)}@corona.test")
        self.grant(user, *KEYS, tenant=self.tenant, role_key=f"states-{next(_roles)}",
                   branch=branch)
        return TenantAPIClient(user=user)

    def capital(self, client, amount=5_000_000):
        response = client.post(
            f"/v1/finance/bank-transactions/?entity={self.books.code}",
            {"bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": amount,
             "counter_account": "3100", "transaction_date": "2026-01-15",
             "narration": "Owner capital"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]

    def transactions(self, client):
        return client.get(f"/v1/finance/bank-transactions/?entity={self.books.code}")

    def route(self, document_type):
        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type=document_type,
            code="standard", name="Bank approval")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Second signature", order=10,
            approver_role_key="finance-approver")

    def test_a_transaction_posted_at_once_names_its_branch_and_was_never_submitted(self):
        client = self.bursar(self.ikeja)
        created = self.capital(client)

        listed = self.transactions(client).data["data"][0]
        detail = client.get(
            f"/v1/finance/bank-transactions/{created['id']}/?entity={self.books.code}").data["data"]

        for row in (created, listed, detail):
            self.assertEqual(row["branch_name"], "Ikeja Branch")
            self.assertEqual(row["approval_state"], "NOT_SUBMITTED")

    def test_a_held_transaction_is_pending_then_reads_as_decided(self):
        self.route("finance.bank_transaction")
        client = self.bursar(self.ikeja)
        created = self.capital(client)

        self.assertEqual(created["approval_state"], "PENDING")
        self.assertEqual(self.transactions(client).data["data"][0]["approval_state"], "PENDING")
        WorkflowInstance.all_objects.filter(document_object_id=str(created["id"])).update(
            status="REJECTED")
        self.assertEqual(self.transactions(client).data["data"][0]["approval_state"], "REJECTED")

    def test_the_list_costs_the_same_queries_for_one_row_or_three(self):
        self.route("finance.bank_transaction")
        client = self.bursar(None)
        self.capital(client)
        with CaptureQueriesContext(connection) as one:
            self.transactions(client)
        self.capital(client, 6_000_000)
        self.capital(client, 7_000_000)
        with CaptureQueriesContext(connection) as three:
            response = self.transactions(client)

        self.assertEqual(len(response.data["data"]), 3)
        self.assertEqual(len(three.captured_queries), len(one.captured_queries))

    def test_a_transfer_names_its_branch_and_its_approval_state(self):
        client = self.bursar(self.ikeja)
        made = client.post(
            f"/v1/finance/bank-transfers/?entity={self.books.code}",
            {"from_account": self.ikeja_zenith.pk, "to_account": self.ikeja_bank.pk,
             "amount": 2_000_000, "transfer_date": "2026-01-15", "narration": "Fund GTBank"},
            format="json",
        )
        self.assertEqual(made.status_code, 201, made.data)

        row = client.get(
            f"/v1/finance/bank-transfers/?entity={self.books.code}").data["data"][0]

        self.assertEqual(row["branch_name"], "Ikeja Branch")
        self.assertEqual(row["approval_state"], "NOT_SUBMITTED")

    def test_another_branch_sees_none_of_ikejas_transactions(self):
        self.capital(self.bursar(self.ikeja))

        self.assertEqual(self.transactions(self.bursar(self.lekki)).data["data"], [])
