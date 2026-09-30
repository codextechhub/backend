"""Pickers and forms can tell whose a row is.

Corona has Ikeja, Lekki and Yaba. The Okafor family is filed under Ikeja and the
Adeyemi family is shared by every branch. A form raising a document against the
Okafors takes Ikeja without asking, and one against the Adeyemis asks which
branch; a deposit picker on an Ikeja receipt offers Ikeja's collection ledger, not
Lekki's; a branch clerk's payments pickers offer only what she may use.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, BankAccount, Customer

from .tests_branch_scope import _FinanceBranchFixture


class _PickerFixture(_FinanceBranchFixture):
    def setUp(self):
        super().setUp()
        self.okafor = self.customer(self.books, "COKAF", self.ikeja)
        self.adeyemi = self.customer(self.books, "CADEY", None)

    def client_for(self, email, *keys, branches=()):
        user = self.user_for(self.tenant, email)
        if not branches:
            self.grant(user, *keys, tenant=self.tenant, role_key=f"role-{email}")
        for n, branch in enumerate(branches):
            self.grant(user, *keys, tenant=self.tenant, role_key=f"role-{email}-{n}", branch=branch)
        return TenantAPIClient(user=user)

    def rows(self, client, path):
        response = client.get(f"/v1/finance/{path}{'&' if '?' in path else '?'}entity={self.books.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        body = response.json()
        return body["data"]


class CustomerRowsCarryTheirBranchTests(_PickerFixture):
    """A form asks for a branch only for a customer every branch shares."""

    def test_the_list_says_which_customers_are_filed_and_which_are_shared(self):
        hq = self.client_for("pick-hq@corona.test", "finance.customer.view")
        rows = {row["code"]: row for row in self.rows(hq, "customers/")}

        self.assertEqual((rows["COKAF"]["branch_id"], rows["COKAF"]["branch_name"]),
                         (self.ikeja.pk, "Ikeja Branch"))
        self.assertIsNone(rows["CADEY"]["branch_id"])

    def test_the_detail_says_so_too(self):
        hq = self.client_for("pick-hq2@corona.test", "finance.customer.view")
        data = hq.get(f"/v1/finance/customers/COKAF/?entity={self.books.code}").json()["data"]

        self.assertEqual(data["customer"]["branch_id"], self.ikeja.pk)
