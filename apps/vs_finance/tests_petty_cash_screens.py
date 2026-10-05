"""What the petty cash screens read: names, branches, a branch filter and the route.

Corona runs Ikeja, Lekki and Yaba. Mrs Adeyemi keeps Ikeja's float and counts it
when the head bursar cuts it; Lekki's float is cut by the same bursar. A reader
who cannot list staff still sees who counted, who raised the return and who closed
a fund, by name, with a mark beside anybody who has since left the school, so the
screen never shows "User 7". The returns list narrows to one branch on
``?branch=``, and a branch the reader does not work in is refused exactly as one
that does not exist. The approval route reports the shortage figure the school
actually has, which it may have changed since adopting, and whether anybody is in
its approver group yet.
"""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient, exited_people
from vs_finance.approvals import adopt_petty_cash_return_template, petty_cash_return_route
from vs_finance.constants import WF_PETTY_CASH_RETURN_APPROVER_GROUP
from vs_finance.models import Account, PettyCashFund
from vs_finance.petty_cash import establish_fund
from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

from .tests_bank_account_reach import _bank
from .tests_branch_scope import _FinanceBranchFixture
from .tests_petty_cash_returns import FLOAT, JAN_2, JAN_10, NAIRA, _PettyCashReturnFixture

RETURNS = "/v1/finance/petty-cash-returns/"
ROUTE = "/v1/finance/petty-cash-returns/approval-template/"


class _ScreensFixture(_PettyCashReturnFixture):
    """The head bursar (whole tenant) cuts Ikeja's and Lekki's floats."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.head = cls.grant(
            cls.user_for(cls.tenant, "head.bursar@corona.test"),
            "finance.pettycash.view", "finance.pettycash.return", "finance.pettycash.close",
            "workflow.template.view",
            tenant=cls.tenant, role_key="screens-head",
        )
        cls.head.first_name, cls.head.last_name = "Bola", "Okafor"
        cls.head.save(update_fields=["first_name", "last_name"])
        cls.custodian.first_name, cls.custodian.last_name = "Ronke", "Adeyemi"
        cls.custodian.save(update_fields=["first_name", "last_name"])
        cls.ikeja_reader = cls.grant(
            cls.user_for(cls.tenant, "ikeja.reader@corona.test"), "finance.pettycash.view",
            tenant=cls.tenant, role_key="screens-ikeja", branch=cls.ikeja,
        )

    def setUp(self):
        self.client = TenantAPIClient(user=self.head)

    def get(self, path, client=None, books=None, **params):
        books = books or self.books
        query = "&".join(f"{k}={v}" for k, v in {"entity": books.code, **params}.items())
        return (client or self.client).get(f"{path}?{query}")

    def reduce(self, fund, bank, counted, new_float):
        response = self.client.post(
            f"/v1/finance/petty-cash-funds/{fund.pk}/reduce/?entity={self.books.code}",
            {"counted_amount": counted, "new_float_amount": new_float,
             "bank_account": bank.pk, "return_date": JAN_10.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]


class PeopleOnPettyCashTests(_ScreensFixture):

    def test_a_return_names_its_counter_its_raiser_and_its_branch(self):
        self.reduce(self.fund, self.ikeja_bank, FLOAT, 50_000 * NAIRA)

        row = self.get(RETURNS).data["data"][0]

        self.assertEqual(row["branch_name"], "Ikeja Branch")
        self.assertEqual(row["counted_by_name"], "Ronke Adeyemi")
        self.assertFalse(row["counted_by_is_exited"])
        self.assertEqual(row["created_by_name"], "Bola Okafor")
        self.assertFalse(row["created_by_is_exited"])
        self.assertEqual(row["counted_by_id"], self.custodian.pk)

    def test_a_custodian_who_has_left_is_marked_on_the_return_and_the_fund(self):
        self.reduce(self.fund, self.ikeja_bank, FLOAT, 50_000 * NAIRA)

        with exited_people(self.custodian):
            row = self.get(RETURNS).data["data"][0]
            fund = self.get(f"/v1/finance/petty-cash-funds/{self.fund.pk}/").data["data"]

        self.assertTrue(row["counted_by_is_exited"])
        self.assertFalse(row["created_by_is_exited"])
        self.assertTrue(fund["custodian_is_exited"])
        self.assertIsNone(fund["closed_by_name"])
        self.assertIsNone(fund["closed_by_is_exited"])

    def test_a_closed_fund_names_who_closed_it(self):
        response = self.client.post(
            f"/v1/finance/petty-cash-funds/{self.fund.pk}/close/?entity={self.books.code}",
            {"counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
             "return_date": JAN_10.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        funds = {row["id"]: row for row in self.get("/v1/finance/petty-cash-funds/").data["data"]}

        self.assertEqual(funds[self.fund.pk]["closed_by_name"], "Bola Okafor")
        self.assertFalse(funds[self.fund.pk]["closed_by_is_exited"])
        self.assertIsNone(funds[self.lekki_fund.pk]["closed_by_name"])

    def test_the_list_costs_the_same_queries_for_one_return_or_three(self):
        self.reduce(self.fund, self.ikeja_bank, FLOAT, 80_000 * NAIRA)
        with CaptureQueriesContext(connection) as one:
            self.get(RETURNS)
        self.reduce(self.fund, self.ikeja_bank, 80_000 * NAIRA, 60_000 * NAIRA)
        self.reduce(self.lekki_fund, self.lekki_bank, 20_000 * NAIRA, 10_000 * NAIRA)
        with CaptureQueriesContext(connection) as three:
            response = self.get(RETURNS)

        self.assertEqual(len(response.data["data"]), 3)
        self.assertEqual(len(three.captured_queries), len(one.captured_queries))


class ReturnsBranchFilterTests(_ScreensFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.yaba_reader = cls.grant(
            cls.user_for(cls.tenant, "yaba.reader@corona.test"), "finance.pettycash.view",
            tenant=cls.tenant, role_key="screens-yaba", branch=cls.yaba,
        )

    def setUp(self):
        super().setUp()
        self.reduce(self.fund, self.ikeja_bank, FLOAT, 50_000 * NAIRA)
        self.reduce(self.lekki_fund, self.lekki_bank, 20_000 * NAIRA, 10_000 * NAIRA)

    def test_a_whole_school_reader_narrows_to_one_branch(self):
        lekki = self.get(RETURNS, branch=self.lekki.pk)
        everyone = self.get(RETURNS)

        self.assertEqual(lekki.status_code, 200, lekki.data)
        self.assertEqual([r["fund_id"] for r in lekki.data["data"]], [self.lekki_fund.pk])
        self.assertIn("pagination", lekki.data)
        self.assertEqual(len(everyone.data["data"]), 2)

    def test_a_branch_outside_reach_is_refused_as_an_unknown_one_is(self):
        reader = TenantAPIClient(user=self.ikeja_reader)

        own = self.get(RETURNS, client=reader, branch=self.ikeja.pk)
        lekki = self.get(RETURNS, client=reader, branch=self.lekki.pk)
        unknown = self.get(RETURNS, client=reader, branch=987654)
        rival = self.get(RETURNS, client=reader, branch=self.rival_branch.pk)

        self.assertEqual([r["fund_id"] for r in own.data["data"]], [self.fund.pk])
        self.assertEqual(lekki.status_code, 400)
        self.assertEqual(lekki.data, unknown.data)
        self.assertEqual(rival.status_code, 400)
        self.assertEqual(rival.data, unknown.data)

    def test_a_branch_with_no_returns_answers_an_empty_list(self):
        response = self.get(RETURNS, client=TenantAPIClient(user=self.yaba_reader),
                            branch=self.yaba.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [])

    def test_without_the_view_key_the_list_is_refused(self):
        nobody = self.grant(
            self.user_for(self.tenant, "nokey.reader@corona.test"), "finance.report.view",
            tenant=self.tenant, role_key="screens-nokey",
        )

        response = self.get(RETURNS, client=TenantAPIClient(user=nobody), branch=self.ikeja.pk)

        self.assertEqual(response.status_code, 403)


class OneBranchReturnsFilterTests(_FinanceBranchFixture):

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
        cls.bursar = cls.grant(
            cls.user_for(cls.solo_tenant, "bursar@solo.test"),
            "finance.pettycash.view", "finance.pettycash.return",
            tenant=cls.solo_tenant, role_key="solo-screens", branch=cls.solo_main,
        )

    def test_the_only_branch_filters_to_its_returns(self):
        client = TenantAPIClient(user=self.bursar)
        made = client.post(
            f"/v1/finance/petty-cash-funds/{self.fund.pk}/reduce/?entity={self.solo_books.code}",
            {"counted_amount": FLOAT, "new_float_amount": 40_000 * NAIRA,
             "bank_account": self.bank.pk, "return_date": JAN_10.isoformat()},
            format="json",
        )
        self.assertEqual(made.status_code, 201, made.data)

        response = client.get(
            f"{RETURNS}?entity={self.solo_books.code}&branch={self.solo_main.pk}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["branch_name"], self.solo_main.name)


class RouteReportsWhatTheSchoolHasTests(_ScreensFixture):

    def route(self):
        response = self.get(ROUTE)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_before_adopting_it_offers_the_default_and_an_absent_group(self):
        data = self.route()

        self.assertFalse(data["adopted"])
        self.assertEqual(data["threshold"], 5_000 * NAIRA)
        self.assertEqual(data["default_threshold"], 5_000 * NAIRA)
        self.assertIsNone(data["approver_group_id"])
        self.assertEqual(data["approver_group_member_count"], 0)

    def test_adopted_at_its_own_figure_it_reports_that_figure(self):
        adopt_petty_cash_return_template(self.tenant, threshold=50_000 * NAIRA)

        data = self.route()

        self.assertTrue(data["adopted"])
        self.assertEqual(data["threshold"], 50_000 * NAIRA)
        self.assertEqual(data["threshold_naira"], "₦50,000.00")
        self.assertEqual(data["default_threshold"], 5_000 * NAIRA)
        self.assertEqual(data["approver_group_member_count"], 0)

    def test_a_figure_changed_on_the_approval_screens_is_the_one_reported(self):
        adopt_petty_cash_return_template(self.tenant)
        stage = petty_cash_return_route(self.tenant).stages.get()
        stage.inclusion_condition = {"any": [
            {"op": "gt", "field": "shortage", "value": 20_000 * NAIRA},
            {"op": "eq", "field": "kind", "value": "CLOSE"},
        ]}
        stage.save(update_fields=["inclusion_condition"])

        self.assertEqual(self.route()["threshold"], 20_000 * NAIRA)

    def test_a_route_no_longer_testing_the_shortage_reports_no_figure(self):
        adopt_petty_cash_return_template(self.tenant)
        stage = petty_cash_return_route(self.tenant).stages.get()
        stage.inclusion_condition = {"op": "eq", "field": "kind", "value": "CLOSE"}
        stage.save(update_fields=["inclusion_condition"])

        data = self.route()

        self.assertTrue(data["adopted"])
        self.assertIsNone(data["threshold"])
        self.assertIsNone(data["threshold_naira"])

    def test_the_member_count_follows_the_group(self):
        adopt_petty_cash_return_template(self.tenant)
        group = WorkflowApproverGroup.all_objects.get(
            tenant=self.tenant, code=WF_PETTY_CASH_RETURN_APPROVER_GROUP)
        self.assertEqual(self.route()["approver_group_id"], group.pk)

        WorkflowApproverGroupMember.objects.create(group=group, kind="USER", user=self.head)

        self.assertEqual(self.route()["approver_group_member_count"], 1)

    def test_another_tenants_group_is_not_counted(self):
        adopt_petty_cash_return_template(self.tenant)
        rival_group, _ = WorkflowApproverGroup.all_objects.get_or_create(
            tenant=self.rival_tenant, code=WF_PETTY_CASH_RETURN_APPROVER_GROUP,
            defaults={"name": "Rival approvers"})
        rival_user = self.user_for(self.rival_tenant, "rival.approver@rival.test")
        WorkflowApproverGroupMember.objects.create(group=rival_group, kind="USER", user=rival_user)

        self.assertEqual(self.route()["approver_group_member_count"], 0)

    def test_without_the_route_view_key_it_is_refused(self):
        response = self.get(ROUTE, client=TenantAPIClient(user=self.ikeja_reader))

        self.assertEqual(response.status_code, 403)
