"""The ready-made approval route for petty cash returns, which a tenant may adopt.

The route asks a second person to approve a return whose count came up more than
₦5,000 short, and every closure. It governs nobody until a tenant adopts it: with
no route, Mrs Adeyemi's count at Corona's Ikeja branch that is ₦26,000 short posts
on her word. Once Corona adopts it, the same count waits for approval, while a
₦200 shortage still posts at once. Adopting binds every branch, so it needs the
key that publishes approval routes and a caller who reaches the whole tenant; at
a tenant with one branch, a caller pinned to that branch does.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.approvals import PETTY_CASH_RETURN_DOCUMENT_TYPE, petty_cash_return_route
from vs_finance.constants import DocumentStatus, WF_PETTY_CASH_RETURN_APPROVER_GROUP
from vs_finance.models import PettyCashReturn
from vs_workflow.models import WorkflowApproverGroup, WorkflowTemplate

from vs_finance.models import Account, PettyCashFund
from vs_finance.petty_cash import establish_fund

from .tests_bank_account_reach import _bank
from .tests_branch_scope import _FinanceBranchFixture
from .tests_petty_cash_returns import FLOAT, JAN_2, JAN_10, NAIRA, _PettyCashReturnFixture

ROUTE = "petty-cash-returns/approval-template/"
PUBLISH = "workflow.template.publish"


class _RouteFixture(_PettyCashReturnFixture):
    """Corona's officers: one for the whole tenant, one pinned to Ikeja."""

    def officer(self, *keys, branch=None, tenant=None, tag=""):
        tenant = tenant or self.tenant
        user = self.grant(
            self.user_for(tenant, f"route-{tag}@corona.test"),
            "finance.pettycash.view", "finance.pettycash.return", "finance.pettycash.close",
            "workflow.template.view", *keys,
            tenant=tenant, role_key=f"route-{tag}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def call(self, client, path, body=None, *, entity=None):
        entity = entity or self.books
        return client.post(f"/v1/finance/{path}?entity={entity.code}", body or {}, format="json")

    def short_count(self, client, shortage):
        """Cut Ikeja's ₦100,000 float to ₦50,000 with a count ``shortage`` kobo short."""
        counted = 100_000 * NAIRA - shortage
        return self.call(client, f"petty-cash-funds/{self.fund.pk}/reduce/", {
            "counted_amount": counted, "new_float_amount": 50_000 * NAIRA,
            "bank_account": self.ikeja_bank.pk, "return_date": JAN_10.isoformat(),
            "difference_reason": "Counted by Mrs Adeyemi with the head bursar",
        })


class TheRouteIsInertUntilAdoptedTests(_RouteFixture):

    def test_no_tenant_and_no_platform_row_routes_returns(self):
        self.assertFalse(WorkflowTemplate.all_objects.filter(
            document_type=PETTY_CASH_RETURN_DOCUMENT_TYPE).exists())

    def test_a_large_shortage_posts_directly_with_no_route(self):
        response = self.short_count(self.officer(tag="inert"), 26_000 * NAIRA)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], DocumentStatus.POSTED)

    def test_the_offer_shows_its_step_and_that_nobody_has_adopted_it(self):
        client = self.officer(tag="look")

        response = client.get(f"/v1/finance/{ROUTE}?entity={self.books.code}")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertFalse(data["adopted"])
        self.assertEqual(data["threshold"], 5_000 * NAIRA)
        self.assertEqual(data["stages"][0]["code"], "second-approval")


class AdoptingTheRouteTests(_RouteFixture):

    def adopt(self, **body):
        response = self.call(self.officer(PUBLISH, tag=f"adopt-{len(body)}"), ROUTE, body)
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def test_adopting_publishes_one_step_and_an_empty_group(self):
        response = self.adopt()

        self.assertTrue(response.data["data"]["adopted"])
        route = petty_cash_return_route(self.tenant)
        self.assertEqual(route.stages.count(), 1)
        group = WorkflowApproverGroup.objects.get(
            tenant=self.tenant, code=WF_PETTY_CASH_RETURN_APPROVER_GROUP)
        self.assertFalse(group.members.exists())

    def test_a_26000_shortage_waits_for_approval(self):
        self.adopt()

        response = self.short_count(self.officer(tag="big"), 26_000 * NAIRA)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        self.assertIsNone(PettyCashReturn.objects.get().journal_id)
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.float_amount, 100_000 * NAIRA)

    def test_a_200_shortage_posts_at_once(self):
        self.adopt()

        response = self.short_count(self.officer(tag="small"), 200 * NAIRA)

        self.assertEqual(response.data["data"]["status"], DocumentStatus.POSTED)

    def test_a_closure_waits_for_approval(self):
        self.adopt()

        response = self.call(self.officer(tag="closing"), f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": 100_000 * NAIRA, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_10.isoformat(),
        })

        self.assertEqual(response.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)

    def test_a_tenant_may_choose_its_own_threshold(self):
        self.adopt(threshold=50_000 * NAIRA)

        response = self.short_count(self.officer(tag="own"), 26_000 * NAIRA)

        self.assertEqual(response.data["data"]["status"], DocumentStatus.POSTED)

    def test_adopting_again_leaves_an_edited_route_alone(self):
        self.adopt()
        stage = petty_cash_return_route(self.tenant).stages.get()
        stage.label = "Head bursar signs off"
        stage.save(update_fields=["label"])

        again = self.call(self.officer(PUBLISH, tag="again"), ROUTE, {"threshold": 0})

        self.assertEqual(again.status_code, 200, again.data)
        stage.refresh_from_db()
        self.assertEqual(stage.label, "Head bursar signs off")

    def test_another_tenant_is_not_governed_by_coronas_route(self):
        self.adopt()

        self.assertIsNone(petty_cash_return_route(self.solo_tenant))
        self.assertIsNone(petty_cash_return_route(self.rival_tenant))


class WhoMayAdoptTests(_RouteFixture):

    def test_without_the_publish_key_adopting_is_refused(self):
        response = self.call(self.officer(tag="nokey"), ROUTE)

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(petty_cash_return_route(self.tenant))

    def test_a_branch_pinned_officer_cannot_adopt_for_every_branch(self):
        response = self.call(self.officer(PUBLISH, branch=self.ikeja, tag="ikeja"), ROUTE)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("SHARED_RECORD_READ_ONLY", str(response.data))
        self.assertIsNone(petty_cash_return_route(self.tenant))

    def test_another_tenant_cannot_adopt_for_corona(self):
        client = self.officer(
            PUBLISH, branch=self.rival_branch, tenant=self.rival_tenant, tag="rival")

        response = self.call(client, ROUTE)

        self.assertIn(response.status_code, (403, 404))
        self.assertIsNone(petty_cash_return_route(self.tenant))


class OneBranchAdoptionTests(_FinanceBranchFixture):
    """At a tenant with one branch, the bursar pinned to it reaches the whole tenant."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.solo_books
        cls.bank = _bank(e, "Main Operations", None, "50")
        cls.fund = PettyCashFund.objects.create(
            entity=e, name="Front desk", float_amount=FLOAT,
            gl_account=Account.objects.get(entity=e, code="1110"),
        )
        establish_fund(cls.fund, bank_account=cls.bank, amount=FLOAT, date=JAN_2)

    def test_the_only_branchs_bursar_adopts_and_a_large_shortage_then_waits(self):
        user = self.grant(
            self.user_for(self.solo_tenant, "head@solo.test"),
            "finance.pettycash.view", "finance.pettycash.return", "workflow.template.view",
            PUBLISH, tenant=self.solo_tenant, role_key="solo-head", branch=self.solo_main,
        )
        client = TenantAPIClient(user=user)
        url = f"/v1/finance/{{}}?entity={self.solo_books.code}"

        adopted = client.post(url.format(ROUTE), {}, format="json")
        self.assertEqual(adopted.status_code, 201, adopted.data)

        response = client.post(
            url.format(f"petty-cash-funds/{self.fund.pk}/reduce/"),
            {"counted_amount": 74_000 * NAIRA, "new_float_amount": 50_000 * NAIRA,
             "bank_account": self.bank.pk, "return_date": JAN_10.isoformat(),
             "difference_reason": "Short at the count"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
