"""An inter-branch send whose approval ends unapproved leaves its owner something to do.

Corona's route sends money leaving a branch to a second signature, Mr Adeyemi.
Three things end an approval without approving it: Mr Adeyemi rejects it, the
bursar who sent it withdraws it, or an administrator cancels it. Each time:

* Ikeja's bursar, Mrs Okafor, sent Lekki 1m unasked. The send is over and reads
  ``NOT_SENT``; nothing was booked, and she sends again with a new transfer.
* Lekki asked Ikeja for the 1m and Ikeja sent it. Lekki's request still stands:
  it reads ``REQUESTED`` again, and Ikeja sends it again or declines it.
* Ikeja forwarded the 400k Mrs Adeyemi paid into Ikeja's account for her son's
  Lekki fees. The forward is over (``NOT_SENT``), so the held receipt is no
  longer being forwarded: Ikeja forwards it again, or voids it.

Single Site has one branch and has nothing to send between branches; Rival
Group's bursar reaches none of Corona's transfers.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import cancel, record_action, withdraw

from .constants import DocumentStatus
from .models import HeldForBranchReceipt, InterBranchTransfer
from .tests_inter_branch import _InterBranchFixture

_seq = itertools.count(1)
ENDINGS = ("rejected", "withdrawn", "cancelled")


class _ApprovalEndFixture(_InterBranchFixture):

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import TenantRoleTemplate

        super().setUpTestData()
        cls.okafor = cls.bursar(cls.ikeja)
        cls.lekki_bursar = cls.bursar(cls.lekki)
        cls.adeyemi_signer = cls.user_for(cls.tenant, f"signer-{next(_seq)}@corona.test")
        cls.grant(cls.adeyemi_signer, tenant=cls.tenant, role_key="finance-approver")
        TenantRoleTemplate.objects.filter(tenant=cls.tenant, key="finance-approver").update(
            is_system_role=True)
        cls.admin = cls.user_for(cls.tenant, f"admin-{next(_seq)}@corona.test")
        template = WorkflowTemplate.objects.create(
            tenant=cls.tenant, branch=None, document_type="finance.inter_branch_transfer",
            code="standard", name="Money leaving a branch")
        WorkflowStage.objects.create(
            template=template, code="signer", label="Second signature", order=10,
            approver_role_key="finance-approver")
        cls.adeyemi = cls.customer(cls.books, "ADEYEMI", cls.lekki)
        cls.lekki_invoice = cls.posted_invoice(cls.adeyemi, cls.lekki, amount=400_000_00)

    def end(self, pk, how):
        """End ``pk``'s approval the way ``how`` names."""
        instance = WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.inter_branch_transfer",
        ).order_by("-created_at").first()
        if how == "rejected":
            record_action(instance.id, self.adeyemi_signer, WorkflowStageAction.REJECTED,
                          comment="Not this month")
        elif how == "withdrawn":
            withdraw(instance.id, instance.requested_by)
        else:
            cancel(instance.id, self.admin, "Raised twice")

    def row(self, client, pk):
        response = client.get(self.url(f"inter-branch-transfers/{pk}/"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def waiting(self, response):
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["stage"], "PENDING_APPROVAL")
        return response.data["data"]["id"]


class UnaskedSendTests(_ApprovalEndFixture):

    def test_an_unasked_send_whose_approval_ends_is_not_sent_and_can_be_sent_anew(self):
        for how in ENDINGS:
            with self.subTest(how=how):
                okafor = self._client(self.okafor)
                pk = self.waiting(self.send(okafor, source=self.ikeja_bank, to_branch=self.lekki))

                self.end(pk, how)

                row = self.row(okafor, pk)
                self.assertEqual((row["status"], row["stage"]), ("CANCELLED", "NOT_SENT"))
                self.assertEqual(row["approval_state"],
                                 "REJECTED" if how == "rejected" else "NOT_SUBMITTED")
                self.assertEqual(self.row(self._client(self.lekki_bursar), pk)["stage"], "NOT_SENT")
                resend = okafor.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                                     {"from_bank_account": self.ikeja_bank.pk}, format="json")
                self.assertEqual(resend.status_code, 400, resend.data)
                self.waiting(self.send(okafor, source=self.ikeja_bank, to_branch=self.lekki))
                self.assertFalse(InterBranchTransfer.objects.get(pk=pk).legs.exists())

    @staticmethod
    def _client(user):
        return TenantAPIClient(user=user)


class RequestedSendTests(_ApprovalEndFixture):

    def ask(self):
        asked = self.client_for(self.lekki).post(self.url("inter-branch-transfers/requests/"), {
            "from_branch": self.ikeja.pk, "amount": 1_000_000_00, "purpose": "Diesel",
            "transfer_date": "2026-01-15",
        }, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        return asked.data["data"]["id"]

    def test_a_request_whose_send_is_not_approved_stands_again(self):
        okafor = TenantAPIClient(user=self.okafor)
        for how in ENDINGS:
            with self.subTest(how=how):
                pk = self.ask()
                self.waiting(okafor.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                                         {"from_bank_account": self.ikeja_bank.pk}, format="json"))

                self.end(pk, how)

                row = self.row(okafor, pk)
                self.assertEqual((row["status"], row["stage"]), ("DRAFT", "REQUESTED"))
                again = okafor.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                                    {"from_bank_account": self.ikeja_bank.pk}, format="json")
                self.assertEqual(again.data["data"]["stage"], "PENDING_APPROVAL", again.data)
                self.end(pk, how)
                declined = okafor.post(self.url(f"inter-branch-transfers/{pk}/decline/"),
                                       {"reason": "Short ourselves"}, format="json")
                self.assertEqual(declined.status_code, 200, declined.data)
                self.assertEqual(declined.data["data"]["stage"], "DECLINED")

    def test_the_asking_branch_cannot_send_its_own_request(self):
        pk = self.ask()
        self.waiting(TenantAPIClient(user=self.okafor).post(
            self.url(f"inter-branch-transfers/{pk}/send/"),
            {"from_bank_account": self.ikeja_bank.pk}, format="json"))
        self.end(pk, "withdrawn")

        refused = TenantAPIClient(user=self.lekki_bursar).post(
            self.url(f"inter-branch-transfers/{pk}/send/"),
            {"from_bank_account": self.lekki_bank.pk}, format="json")

        self.assertEqual(refused.status_code, 403, refused.data)


class ForwardTests(_ApprovalEndFixture):

    def hold(self, client):
        held = client.post(self.url("held-receipts/"), {
            "bank_account": self.ikeja_bank.pk, "for_branch": self.lekki.pk,
            "customer": "ADEYEMI", "amount": 400_000_00, "receipt_date": "2026-01-15",
        }, format="json")
        self.assertEqual(held.status_code, 201, held.data)
        return held.data["data"]["id"]

    def forward(self, client, held):
        return client.post(self.url(f"held-receipts/{held}/forward/"),
                           {"transfer_date": "2026-01-16"}, format="json")

    def test_a_forward_whose_approval_ends_frees_the_receipt_to_forward_again(self):
        okafor = TenantAPIClient(user=self.okafor)
        for how in ENDINGS:
            with self.subTest(how=how):
                held = self.hold(okafor)
                pk = self.waiting(self.forward(okafor, held))
                self.assertIsNotNone(
                    okafor.get(self.url(f"held-receipts/{held}/")).data["data"]["forwarded_by"])

                self.end(pk, how)

                self.assertEqual(self.row(okafor, pk)["stage"], "NOT_SENT")
                receipt = okafor.get(self.url(f"held-receipts/{held}/")).data["data"]
                self.assertIsNone(receipt["forwarded_by"])
                again = self.waiting(self.forward(okafor, held))
                self.assertNotEqual(again, pk)

    def test_a_forward_whose_approval_ends_lets_the_receipt_be_voided(self):
        okafor = TenantAPIClient(user=self.okafor)
        for how in ENDINGS:
            with self.subTest(how=how):
                held = self.hold(okafor)
                pk = self.waiting(self.forward(okafor, held))

                self.end(pk, how)

                voided = okafor.post(self.url(f"held-receipts/{held}/void/"), {}, format="json")
                self.assertEqual(voided.status_code, 200, voided.data)
                self.assertEqual(HeldForBranchReceipt.objects.get(pk=held).status,
                                 DocumentStatus.REVERSED)
                self.assertEqual(InterBranchTransfer.objects.get(pk=pk).status,
                                 DocumentStatus.CANCELLED)

    def test_another_school_cannot_see_the_forward(self):
        okafor = TenantAPIClient(user=self.okafor)
        pk = self.waiting(self.forward(okafor, self.hold(okafor)))
        self.end(pk, "withdrawn")
        rival = self.bursar(None, tenant=self.rival_tenant)

        response = TenantAPIClient(user=rival).get(self.url(f"inter-branch-transfers/{pk}/"))

        self.assertIn(response.status_code, (403, 404), response.data)


class OneBranchTests(_ApprovalEndFixture):

    def test_a_one_branch_school_has_nothing_to_send(self):
        solo = TenantAPIClient(user=self.bursar(None, tenant=self.solo_tenant))

        response = solo.post(self.url("inter-branch-transfers/", books=self.solo_books), {
            "from_bank_account": self.solo_bank.pk, "to_branch": self.solo_main.pk,
            "amount": 1_000, "transfer_date": "2026-01-15", "purpose": "Diesel",
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.solo_books).exists())
