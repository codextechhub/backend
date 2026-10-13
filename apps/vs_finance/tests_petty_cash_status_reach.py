"""The petty cash status screen shows a branch-bound reader their own branches' floats only.

A fund's float and the cash on hand are its branch's money. Mrs Adeyemi keeps
Ikeja's float and the Lekki bursar keeps Lekki's: the status screen answers
each of them with their own branch's fund, and the head bursar, who covers the
whole school, with both. A school with one branch loses nothing, and another
school's funds are never served.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient

from .tests_petty_cash_field_access import _PettyCashFieldAccess

STATUS = "/v1/finance/petty-cash-status/"
VIEW = ("finance.pettycash.view",)


class PettyCashStatusReachTests(_PettyCashFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.head_reader = cls.grant(
            cls.user_for(cls.tenant, "status.head@corona.test"), *VIEW,
            tenant=cls.tenant, role_key="status-head",
        )
        cls.ikeja_reader = cls.grant(
            cls.user_for(cls.tenant, "status.ikeja@corona.test"), *VIEW,
            tenant=cls.tenant, role_key="status-ikeja", branch=cls.ikeja,
        )
        cls.lekki_reader = cls.grant(
            cls.user_for(cls.tenant, "status.lekki@corona.test"), *VIEW,
            tenant=cls.tenant, role_key="status-lekki", branch=cls.lekki,
        )
        cls.yaba_reader = cls.grant(
            cls.user_for(cls.tenant, "status.yaba@corona.test"), *VIEW,
            tenant=cls.tenant, role_key="status-yaba", branch=cls.yaba,
        )
        cls.solo_head = cls.grant(
            cls.user_for(cls.solo_tenant, "status.head@solo.test"), *VIEW,
            tenant=cls.solo_tenant, role_key="status-solo",
        )
        cls.solo_pinned = cls.grant(
            cls.user_for(cls.solo_tenant, "status.pinned@solo.test"), *VIEW,
            tenant=cls.solo_tenant, role_key="status-solo-pinned", branch=cls.solo_main,
        )

    def rows(self, user, books=None):
        response = TenantAPIClient(user=user).get(
            f"{STATUS}?entity={(books or self.books).code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["rows"]

    def names(self, user, books=None):
        return sorted(row["name"] for row in self.rows(user, books))

    def test_the_head_bursar_reads_every_branchs_float(self):
        self.assertEqual(self.names(self.head_reader), ["Ikeja front desk", "Lekki front desk"])

    def test_a_branch_reader_reads_only_their_own_branchs_float(self):
        self.assertEqual(self.names(self.ikeja_reader), ["Ikeja front desk"])
        self.assertEqual(self.names(self.lekki_reader), ["Lekki front desk"])

    def test_a_branch_reader_never_reads_the_other_branchs_cash_on_hand(self):
        (row,) = self.rows(self.lekki_reader)

        self.assertEqual(row["float_amount"]["kobo"], 20_000 * 100)
        self.assertNotIn(self.fund.name, {r["name"] for r in self.rows(self.lekki_reader)})

    def test_a_branch_without_a_fund_reads_none(self):
        self.assertEqual(self.rows(self.yaba_reader), [])

    def test_a_single_branch_school_loses_nothing_whether_or_not_the_reader_is_pinned(self):
        self.assertEqual(self.names(self.solo_head, self.solo_books), ["Front desk"])
        self.assertEqual(self.names(self.solo_pinned, self.solo_books), ["Front desk"])

    def test_another_schools_funds_are_never_served(self):
        self.assertEqual(
            TenantAPIClient(user=self.head_reader).get(
                f"{STATUS}?entity={self.rival_books.code}").status_code in (403, 404), True)
