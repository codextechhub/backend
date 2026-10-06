"""A petty cash return its approver sent back reads so, and is not cancelled underneath its request.

Mrs Adeyemi closes Ikeja's petty cash fund; the closure waits for approval, and
the approver returns it to her to recount. The returns list and the return's
detail say it is back with her (``approval_state`` PENDING,
``approval_returned`` true), and the detail names the request she resumes
(``workflow_instance_id``). "Cancel return" is refused while that request is
open, telling her to withdraw it first; withdrawing it cancels the closure, as
the route always has. Lekki's officer does not reach Ikeja's return at all.
"""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_workflow.constants import WorkflowStageAction
from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
from vs_workflow.services.actions import record_action, withdraw

from .constants import DocumentStatus
from .models import PettyCashReturn
from .tests_petty_cash_returns import FLOAT, JAN_20, NAIRA, _PettyCashReturnFixture

KEYS = ("finance.pettycash.view", "finance.pettycash.close", "finance.pettycash.return",
        "finance.pettycash.reverse")


class _SentBackReturnFixture(_PettyCashReturnFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        template = WorkflowTemplate.objects.create(
            tenant=cls.tenant, branch=None, document_type="finance.petty_cash_return",
            code="standard", name="Petty cash returns")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Approval", order=10,
            approver_role_key="finance-approver")
        cls.approver = cls.grant(cls.user_for(cls.tenant, "pc-approver@corona.test"),
                                 tenant=cls.tenant, role_key="finance-approver")
        TenantRoleTemplate.objects.filter(tenant=cls.tenant, key="finance-approver").update(
            is_system_role=True)
        cls.officer = cls.grant(cls.user_for(cls.tenant, "pc-ikeja@corona.test"), *KEYS,
                                tenant=cls.tenant, role_key="pc-ikeja", branch=cls.ikeja)
        cls.lekki_officer = cls.grant(cls.user_for(cls.tenant, "pc-lekki@corona.test"), *KEYS,
                                      tenant=cls.tenant, role_key="pc-lekki", branch=cls.lekki)

    def setUp(self):
        self.client_ = TenantAPIClient(user=self.officer)

    def url(self, path):
        return f"/v1/finance/{path}?entity={self.books.code}"

    def held_reduction(self, counted):
        """A reduction of Ikeja's float, waiting for approval."""
        self.fund.refresh_from_db()
        held = self.client_.post(self.url(f"petty-cash-funds/{self.fund.pk}/reduce/"), {
            "counted_amount": counted, "new_float_amount": 50_000 * NAIRA,
            "bank_account": self.ikeja_bank.pk, "return_date": JAN_20.isoformat(),
            **({"difference_reason": "Recounted"} if counted != FLOAT else {}),
        }, format="json")
        self.assertEqual(held.status_code, 201, held.data)
        return held.data["data"]["id"]

    def held_closure(self):
        held = self.client_.post(self.url(f"petty-cash-funds/{self.fund.pk}/close/"), {
            "counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_20.isoformat(),
        }, format="json")
        self.assertEqual(held.status_code, 201, held.data)
        self.assertEqual(held.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        return held.data["data"]["id"]

    def request_of(self, pk):
        return WorkflowInstance.all_objects.filter(
            document_object_id=str(pk), document_type="finance.petty_cash_return",
        ).order_by("-created_at").first()

    def send_back(self, pk):
        instance = self.request_of(pk)
        record_action(instance.id, self.approver, WorkflowStageAction.RETURNED, comment="Recount")
        return instance


class ASentBackReturnReadsSoTests(_SentBackReturnFixture):

    def test_the_list_and_detail_say_it_is_back_with_its_sender(self):
        pk = self.held_closure()
        instance = self.send_back(pk)

        row = next(r for r in self.client_.get(self.url("petty-cash-returns/")).data["data"]
                   if r["id"] == pk)
        detail = self.client_.get(self.url(f"petty-cash-returns/{pk}/"))

        self.assertEqual((row["approval_state"], row["approval_returned"]), ("PENDING", True))
        self.assertNotIn("workflow_instance_id", row)
        self.assertEqual(detail.status_code, 200, detail.data)
        data = detail.data["data"]
        self.assertEqual((data["approval_state"], data["approval_returned"]), ("PENDING", True))
        self.assertEqual(data["workflow_instance_id"], str(instance.id))

    def test_one_never_sent_reads_not_submitted_with_no_request(self):
        from .petty_cash import post_petty_cash_return
        from .constants import PettyCashReturnKind

        ret = self.make_return(PettyCashReturnKind.REDUCE, counted=FLOAT,
                               amount=50_000 * NAIRA, new_float=50_000 * NAIRA)
        post_petty_cash_return(ret)

        data = self.client_.get(self.url(f"petty-cash-returns/{ret.pk}/")).data["data"]

        self.assertEqual((data["approval_state"], data["approval_returned"]),
                         ("NOT_SUBMITTED", False))
        self.assertIsNone(data["workflow_instance_id"])

    def test_the_list_reads_approval_once_a_page(self):
        first = self.held_reduction(FLOAT - 100 * NAIRA)
        self.send_back(first)
        with CaptureQueriesContext(connection) as one:
            self.client_.get(self.url("petty-cash-returns/"))
        withdraw(self.request_of(first).id, self.officer)
        for _ in range(3):
            self.send_back(self.held_reduction(FLOAT - 100 * NAIRA))
            withdraw(self.request_of(PettyCashReturn.objects.latest("pk").pk).id, self.officer)
        with CaptureQueriesContext(connection) as four:
            listed = self.client_.get(self.url("petty-cash-returns/"))

        self.assertEqual(len(listed.data["data"]), 4)
        self.assertEqual(len(four), len(one))

    def test_sent_back_lists_only_the_returned_one(self):
        returned = self.held_closure()
        self.send_back(returned)

        listed = self.client_.get(self.url("petty-cash-returns/") + "&approval=returned")

        self.assertEqual([row["id"] for row in listed.data["data"]], [returned])


class CancellingUnderAnOpenRequestTests(_SentBackReturnFixture):

    def test_cancel_is_refused_while_the_request_is_back_with_its_sender(self):
        pk = self.held_closure()
        instance = self.send_back(pk)

        refused = self.client_.post(self.url(f"petty-cash-returns/{pk}/void/"), {}, format="json")

        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(refused.data["error"]["code"], "APPROVAL_REQUEST_OPEN")
        self.assertIn("Withdraw the approval request instead", refused.data["message"])
        self.assertTrue(refused.data["error"]["detail"]["approval_returned"])
        self.assertEqual(PettyCashReturn.objects.get(pk=pk).status, DocumentStatus.DRAFT)
        instance.refresh_from_db()
        self.assertEqual(instance.status, "RETURNED")

    def test_withdrawing_the_request_ends_it_and_cancels_the_return(self):
        pk = self.held_closure()
        instance = self.send_back(pk)

        withdraw(instance.id, self.officer)

        self.assertEqual(PettyCashReturn.objects.get(pk=pk).status, DocumentStatus.CANCELLED)

    def test_another_branch_does_not_reach_it(self):
        pk = self.held_closure()
        self.send_back(pk)

        response = TenantAPIClient(user=self.lekki_officer).post(
            self.url(f"petty-cash-returns/{pk}/void/"), {}, format="json")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(PettyCashReturn.objects.get(pk=pk).status, DocumentStatus.DRAFT)
