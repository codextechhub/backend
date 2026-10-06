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

        with self.assertRaises(ApprovalRequestOpenError) as refused:
            void_held_receipt(receipt, actor_user=self.okafor)

        self.assert_names_the_way_out(refused.exception, resume=True)
        withdraw(instance.id, instance.requested_by)
        voided = void_held_receipt(receipt, actor_user=self.okafor)
        self.assertEqual(voided.status, "REVERSED")

    def test_a_held_receipt_is_not_voided_while_its_forward_is_with_its_approvers(self):
        receipt, _ = self.forwarded()

        with self.assertRaises(ApprovalRequestOpenError) as refused:
            void_held_receipt(receipt, actor_user=self.okafor)

        self.assert_names_the_way_out(refused.exception, resume=False)

    def test_a_receipt_whose_forward_was_sent_back_is_under_sent_back_not_forwarding(self):
        waiting, _ = self.forwarded()
        returned, forward = self.forwarded()
        self.send_back(forward)

        def listed(**params):
            query = "&".join(f"{key}={value}" for key, value in params.items())
            response = self.okafor_client.get(self.url(f"held-receipts/?{query}"))
            self.assertEqual(response.status_code, 200, response.data)
            return {row["id"]: row for row in response.data["data"]}

        self.assertEqual(set(listed(status="FORWARDING")), {waiting.pk})
        sent_back = listed(approval="returned")
        self.assertEqual(set(sent_back), {returned.pk})
        self.assertTrue(sent_back[returned.pk]["forwarded_by"]["approval_returned"])
        self.assertFalse(listed()[waiting.pk]["forwarded_by"]["approval_returned"])
        wrong = self.okafor_client.get(self.url("held-receipts/?approval=yes"))
        self.assertEqual(wrong.status_code, 400, wrong.data)

    def test_a_void_that_fails_partway_leaves_nothing_behind(self):
        """Voiding is one step: a failure after the reversal posts undoes the reversal too."""
        from unittest import mock

        from .models import JournalEntry

        held = self.okafor_client.post(self.url("held-receipts/"), {
            "bank_account": self.ikeja_bank.pk, "for_branch": self.lekki.pk,
            "customer": "ADEYEMI", "amount": 400_000_00, "receipt_date": "2026-01-15",
        }, format="json")
        receipt = HeldForBranchReceipt.objects.get(pk=held.data["data"]["id"])
        journals = JournalEntry.objects.filter(entity=self.books).count()

        with mock.patch("vs_finance.inter_branch.record", side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                void_held_receipt(receipt, actor_user=self.okafor)

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "POSTED")
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), journals)

    def forwarded(self):
        held = self.okafor_client.post(self.url("held-receipts/"), {
            "bank_account": self.ikeja_bank.pk, "for_branch": self.lekki.pk,
            "customer": "ADEYEMI", "amount": 400_000_00, "receipt_date": "2026-01-15",
        }, format="json")
        held_id = held.data["data"]["id"]
        forward = self.waiting(self.okafor_client.post(
            self.url(f"held-receipts/{held_id}/forward/"), {"transfer_date": "2026-01-16"},
            format="json"))
        return HeldForBranchReceipt.objects.get(pk=held_id), forward

    def assert_names_the_way_out(self, exc, *, resume):
        """The refusal says what ends the forward: never to void a transfer nobody can void."""
        message = str(exc)
        self.assertNotIn("void that transfer", message.lower())
        self.assertIn("Workflow, My Submissions", message)
        self.assertIn("withdraw", message.lower())
        self.assertEqual("resume" in message.lower(), resume)
