"""An inter-branch send sent back to Ikeja's bursar is not ended underneath its request.

Lekki asked Ikeja for 1m; Mrs Okafor sent it and Mr Adeyemi returned the send to
her. While that request is open she cannot decline Lekki's request: she
withdraws the send first, and Lekki's request stands again to be sent or
declined. A send nobody asked for, returned and then withdrawn, is over
(CANCELLED, NOT_SENT) exactly as one withdrawn while with its approver is. A held
receipt whose forward is back with its sender is not voided until that forward's
request is withdrawn.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance
from vs_workflow.services.actions import record_action, withdraw

from .exceptions import ApprovalRequestOpenError
from .inter_branch import void_held_receipt
from .models import HeldForBranchReceipt
from .tests_inter_branch_approval_end import _ApprovalEndFixture


class _SentBackFixture(_ApprovalEndFixture):

    def setUp(self):
        self.okafor_client = TenantAPIClient(user=self.okafor)

    def request_of(self, pk):
        return WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.inter_branch_transfer",
        ).order_by("-created_at").first()

    def send_back(self, pk):
        instance = self.request_of(pk)
        record_action(instance.id, self.adeyemi_signer, WorkflowStageAction.RETURNED,
                      comment="Use the other account")
        return instance


class SentBackSendTests(_SentBackFixture):

    def test_a_request_is_not_declined_while_its_send_is_back_with_the_sender(self):
        asked = self.client_for(self.lekki).post(self.url("inter-branch-transfers/requests/"), {
            "from_branch": self.ikeja.pk, "amount": 1_000_000_00, "purpose": "Diesel",
            "transfer_date": "2026-01-15",
        }, format="json")
        pk = asked.data["data"]["id"]
        self.waiting(self.okafor_client.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                                             {"from_bank_account": self.ikeja_bank.pk},
                                             format="json"))
        instance = self.send_back(pk)

        refused = self.okafor_client.post(self.url(f"inter-branch-transfers/{pk}/decline/"),
                                          {"reason": "Short ourselves"}, format="json")

        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "APPROVAL_REQUEST_OPEN")
        withdraw(instance.id, instance.requested_by)
        self.assertEqual(self.row(self.okafor_client, pk)["stage"], "REQUESTED")
        declined = self.okafor_client.post(self.url(f"inter-branch-transfers/{pk}/decline/"),
                                           {"reason": "Short ourselves"}, format="json")
        self.assertEqual(declined.status_code, 200, declined.data)

    def test_an_unasked_send_sent_back_then_withdrawn_is_over(self):
        pk = self.waiting(self.send(self.okafor_client, source=self.ikeja_bank, to_branch=self.lekki))
        instance = self.send_back(pk)

        withdraw(instance.id, instance.requested_by)

        row = self.row(self.okafor_client, pk)
        self.assertEqual((row["status"], row["stage"]), ("CANCELLED", "NOT_SENT"))


class SentBackForwardTests(_SentBackFixture):

    def test_a_held_receipt_is_not_voided_while_its_forward_is_back_with_the_sender(self):
        held = self.okafor_client.post(self.url("held-receipts/"), {
            "bank_account": self.ikeja_bank.pk, "for_branch": self.lekki.pk,
            "customer": "ADEYEMI", "amount": 400_000_00, "receipt_date": "2026-01-15",
        }, format="json")
        held_id = held.data["data"]["id"]
        forward = self.waiting(self.okafor_client.post(
            self.url(f"held-receipts/{held_id}/forward/"), {"transfer_date": "2026-01-16"},
            format="json"))
        instance = self.send_back(forward)
        receipt = HeldForBranchReceipt.objects.get(pk=held_id)

        with self.assertRaises(ApprovalRequestOpenError):
            void_held_receipt(receipt, actor_user=self.okafor)

        withdraw(instance.id, instance.requested_by)
        voided = void_held_receipt(receipt, actor_user=self.okafor)
        self.assertEqual(voided.status, "REVERSED")
