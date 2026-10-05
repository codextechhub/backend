"""A rejected bank transaction or transfer is fixed and sent again, or cancelled.

Mrs Okafor, Ikeja's bursar, books N50,000 of owner capital into Ikeja's GTBank
account. Corona's route sends bank documents to a second signature, and Mr Adeyemi
rejects it: the owner put in N40,000, not N50,000. The transaction goes back to
being Mrs Okafor's draft. She corrects the amount and submits it again through the
same route, and it posts once approved. Had the money never arrived, she would
cancel the draft instead: it is marked cancelled, the cancellation is audited, and
nothing reaches the books.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch; Rival Group is
another school whose bursar reaches none of Corona's documents.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, FinanceAuditAction
from vs_finance.models import (
    Account,
    BankAccount,
    BankTransaction,
    BankTransfer,
    FinanceAuditLog,
    JournalEntry,
)
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action, withdraw

from .tests_ledger_lock import _LockFixture

_roles = itertools.count(1)
KEYS = (
    "finance.banktransaction.view", "finance.banktransaction.create",
    "finance.banktransfer.view", "finance.banktransfer.create",
)
VIEW_ONLY = ("finance.banktransaction.view", "finance.banktransfer.view")


class _ReworkFixture(_LockFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_zenith = cls.bank("1152", "Ikeja Zenith", cls.ikeja)
        cls.lekki_bank = cls.bank("1153", "Lekki GTBank", cls.lekki)
        solo_ledger = Account.objects.create(
            entity=cls.solo_books, code="1151", name="Main GTBank",
            account_type=cls.acc("1100").account_type, is_postable=True,
        )
        cls.solo_bank = BankAccount.objects.create(
            entity=cls.solo_books, name="Main GTBank", branch=cls.solo_main,
            gl_account=solo_ledger,
        )
        cls.okafor = cls.bursar(cls.tenant, cls.ikeja)
        cls.hq = cls.bursar(cls.tenant, None)
        cls.lekki_bursar = cls.bursar(cls.tenant, cls.lekki)
        cls.viewer = cls.bursar(cls.tenant, cls.ikeja, keys=VIEW_ONLY)
        cls.solo_bursar = cls.bursar(cls.solo_tenant, None)
        cls.rival = cls.bursar(cls.rival_tenant, None)
        cls.adeyemi = cls.approver(cls.tenant)
        cls.solo_approver = cls.approver(cls.solo_tenant)
        for tenant in (cls.tenant, cls.solo_tenant):
            for document_type in ("finance.bank_transaction", "finance.bank_transfer"):
                template = WorkflowTemplate.objects.create(
                    tenant=tenant, branch=None, document_type=document_type,
                    code="standard", name="Bank approval")
                WorkflowStage.objects.create(
                    template=template, code="approver", label="Second signature", order=10,
                    approver_role_key="finance-approver")

    @classmethod
    def bursar(cls, tenant, branch, *, keys=KEYS):
        number = next(_roles)
        user = cls.user_for(tenant, f"rework-{number}@bank.test")
        return cls.grant(user, *keys, tenant=tenant, role_key=f"rework-{number}", branch=branch)

    @classmethod
    def approver(cls, tenant):
        from vs_rbac.models import TenantRoleTemplate

        user = cls.user_for(tenant, f"approver-{next(_roles)}@bank.test")
        cls.grant(user, tenant=tenant, role_key="finance-approver")
        TenantRoleTemplate.objects.filter(tenant=tenant, key="finance-approver").update(
            is_system_role=True)
        return user

    # -- helpers ------------------------------------------------------------- #

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def capital(self, user, *, bank=None, books=None, amount=5_000_000):
        response = TenantAPIClient(user=user).post(
            self.url("bank-transactions/", books),
            {"bank_account": (bank or self.ikeja_bank).pk, "direction": "IN",
             "amount": amount, "counter_account": "3100",
             "transaction_date": "2026-01-15", "narration": "Owner capital"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["approval_state"], "PENDING")
        return response.data["data"]["id"]

    def transfer(self, user):
        response = TenantAPIClient(user=user).post(
            self.url("bank-transfers/"),
            {"from_account": self.ikeja_zenith.pk, "to_account": self.ikeja_bank.pk,
             "amount": 2_000_000, "transfer_date": "2026-01-15", "narration": "Fund GTBank"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]["id"]

    def decide(self, model, pk, action, approver=None):
        instance = WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type=model.workflow_document_type,
        ).order_by("-created_at").first()
        record_action(instance.id, approver or self.adeyemi, action, comment="Wrong amount")

    def reject(self, model, pk, approver=None):
        self.decide(model, pk, WorkflowStageAction.REJECTED, approver)


class BankTransactionReworkTests(_ReworkFixture):

    def test_a_rejected_transaction_is_corrected_and_resubmitted_through_its_route(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.okafor)

        row = client.get(self.url(f"bank-transactions/{pk}/")).data["data"]
        self.assertEqual((row["status"], row["approval_state"]), ("DRAFT", "REJECTED"))

        edited = client.patch(self.url(f"bank-transactions/{pk}/"),
                              {"amount": 4_000_000, "narration": "Owner capital, corrected"},
                              format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data["data"]["amount"], 4_000_000)
        self.assertEqual(edited.data["data"]["status"], "DRAFT")

        sent = client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["data"]["approval_state"], "PENDING")
        self.assertIn("approval", sent.data["data"])
        self.assertEqual(WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.bank_transaction").count(), 2)

        self.decide(BankTransaction, pk, WorkflowStageAction.APPROVED)
        txn = BankTransaction.objects.get(pk=pk)
        self.assertEqual(txn.status, DocumentStatus.POSTED)
        self.assertEqual(sum(txn.journal.lines.values_list("debit", flat=True)), 4_000_000)
        edit = FinanceAuditLog.objects.get(action=FinanceAuditAction.BANK_TRANSACTION_EDITED)
        self.assertEqual((edit.before["amount"], edit.after["amount"]), (5_000_000, 4_000_000))
        self.assertNotIn("direction", edit.after)

    def test_a_rejected_transaction_is_cancelled_audited_and_posts_nothing(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.okafor)

        cancelled = client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json")

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["data"]["status"], "CANCELLED")
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.BANK_TRANSACTION_CANCELLED, target_id=str(pk)).exists())
        for refused in (
            client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json"),
            client.patch(self.url(f"bank-transactions/{pk}/"), {"amount": 1}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json"),
        ):
            self.assertGreaterEqual(refused.status_code, 400, refused.data)
            self.assertLess(refused.status_code, 500, refused.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).status, DocumentStatus.CANCELLED)

    def test_a_transaction_waiting_on_its_approver_cannot_be_changed(self):
        pk = self.capital(self.okafor)
        client = TenantAPIClient(user=self.okafor)

        for refused in (
            client.patch(self.url(f"bank-transactions/{pk}/"), {"amount": 1}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json"),
        ):
            self.assertGreaterEqual(refused.status_code, 400, refused.data)
            self.assertLess(refused.status_code, 500, refused.data)
        txn = BankTransaction.objects.get(pk=pk)
        self.assertEqual((txn.status, txn.amount),
                         (DocumentStatus.PENDING_APPROVAL, 5_000_000))

    def test_a_withdrawn_transaction_is_a_draft_that_can_be_cancelled(self):
        pk = self.capital(self.okafor)
        instance = WorkflowInstance.all_objects.get(document_object_id=str(pk),
                                                    document_type="finance.bank_transaction")
        withdraw(instance.id, self.okafor)
        client = TenantAPIClient(user=self.okafor)

        row = client.get(self.url(f"bank-transactions/{pk}/")).data["data"]
        self.assertEqual((row["status"], row["approval_state"]), ("DRAFT", "NOT_SUBMITTED"))
        cancelled = client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["data"]["status"], "CANCELLED")

    def test_an_edit_answers_to_the_same_branch_reach_as_creating(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)

        moved = TenantAPIClient(user=self.okafor).patch(
            self.url(f"bank-transactions/{pk}/"), {"bank_account": self.lekki_bank.pk},
            format="json")
        self.assertEqual(moved.status_code, 404, moved.data)

        by_hq = TenantAPIClient(user=self.hq).patch(
            self.url(f"bank-transactions/{pk}/"), {"bank_account": self.lekki_bank.pk},
            format="json")
        self.assertEqual(by_hq.status_code, 200, by_hq.data)
        self.assertEqual(by_hq.data["data"]["branch_name"], "Lekki Branch")

    def test_an_edit_is_checked_as_creating_is(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.okafor)

        for body in ({"amount": 0}, {"direction": "SIDEWAYS"}, {"narration": "  "},
                     {"counter_account": "1200"}):
            with self.subTest(body=body):
                response = client.patch(self.url(f"bank-transactions/{pk}/"), body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).amount, 5_000_000)

    def test_another_branchs_bursar_reaches_none_of_it(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.lekki_bursar)

        for response in (
            client.patch(self.url(f"bank-transactions/{pk}/"), {"amount": 1}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json"),
        ):
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).status, DocumentStatus.DRAFT)

    def test_a_reader_without_the_create_key_is_refused(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.viewer)

        for response in (
            client.patch(self.url(f"bank-transactions/{pk}/"), {"amount": 1}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json"),
            client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json"),
        ):
            self.assertEqual(response.status_code, 403, response.data)

    def test_another_schools_bursar_reaches_none_of_it(self):
        pk = self.capital(self.okafor)
        self.reject(BankTransaction, pk)
        client = TenantAPIClient(user=self.rival)

        for books in (self.books, self.rival_books):
            for response in (
                client.patch(self.url(f"bank-transactions/{pk}/", books), {"amount": 1},
                             format="json"),
                client.post(self.url(f"bank-transactions/{pk}/submit/", books), {},
                            format="json"),
                client.post(self.url(f"bank-transactions/{pk}/cancel/", books), {},
                            format="json"),
            ):
                self.assertIn(response.status_code, (403, 404), response.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).status, DocumentStatus.DRAFT)

    def test_a_one_branch_school_corrects_and_resubmits_its_transaction(self):
        pk = self.capital(self.solo_bursar, bank=self.solo_bank, books=self.solo_books)
        self.reject(BankTransaction, pk, self.solo_approver)
        client = TenantAPIClient(user=self.solo_bursar)

        edited = client.patch(self.url(f"bank-transactions/{pk}/", self.solo_books),
                              {"amount": 4_000_000}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data["data"]["branch_name"], "Main Branch")
        sent = client.post(self.url(f"bank-transactions/{pk}/submit/", self.solo_books), {},
                           format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["data"]["approval_state"], "PENDING")


class BankTransferReworkTests(_ReworkFixture):

    def test_a_rejected_transfer_is_corrected_and_resubmitted_through_its_route(self):
        pk = self.transfer(self.okafor)
        self.reject(BankTransfer, pk)
        client = TenantAPIClient(user=self.okafor)

        edited = client.patch(self.url(f"bank-transfers/{pk}/"),
                              {"amount": 1_500_000, "reference": "ZEN-0042"}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual((edited.data["data"]["amount"], edited.data["data"]["reference"]),
                         (1_500_000, "ZEN-0042"))
        sent = client.post(self.url(f"bank-transfers/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(sent.data["data"]["approval_state"], "PENDING")

        self.decide(BankTransfer, pk, WorkflowStageAction.APPROVED)
        transfer = BankTransfer.objects.get(pk=pk)
        self.assertEqual(transfer.status, DocumentStatus.POSTED)
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.BANK_TRANSFER_EDITED, target_id=str(pk)).exists())

    def test_a_rejected_transfer_is_cancelled_audited_and_posts_nothing(self):
        pk = self.transfer(self.okafor)
        self.reject(BankTransfer, pk)

        cancelled = TenantAPIClient(user=self.okafor).post(
            self.url(f"bank-transfers/{pk}/cancel/"), {}, format="json")

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["data"]["status"], "CANCELLED")
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.BANK_TRANSFER_CANCELLED, target_id=str(pk)).exists())

    def test_a_transfer_edit_keeps_both_accounts_in_one_branch(self):
        pk = self.transfer(self.okafor)
        self.reject(BankTransfer, pk)

        across = TenantAPIClient(user=self.hq).patch(
            self.url(f"bank-transfers/{pk}/"), {"to_account": self.lekki_bank.pk}, format="json")

        self.assertEqual(across.status_code, 400, across.data)
        self.assertEqual(BankTransfer.objects.get(pk=pk).to_account_id, self.ikeja_bank.pk)

    def test_another_branch_and_a_reader_and_another_school_are_refused(self):
        pk = self.transfer(self.okafor)
        self.reject(BankTransfer, pk)

        for user, expected in ((self.lekki_bursar, (404,)), (self.viewer, (403,)),
                               (self.rival, (403, 404))):
            client = TenantAPIClient(user=user)
            for response in (
                client.patch(self.url(f"bank-transfers/{pk}/"), {"amount": 1}, format="json"),
                client.post(self.url(f"bank-transfers/{pk}/submit/"), {}, format="json"),
                client.post(self.url(f"bank-transfers/{pk}/cancel/"), {}, format="json"),
            ):
                self.assertIn(response.status_code, expected, response.data)
        self.assertEqual(BankTransfer.objects.get(pk=pk).status, DocumentStatus.DRAFT)
