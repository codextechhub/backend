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


class LedgerAccountsNameTheirBanksBranchTests(_PickerFixture):
    """A deposit picker narrows ledger accounts to the document's branch's banks."""

    def test_each_ledger_account_names_the_branch_of_the_bank_behind_it(self):
        cash_type = Account.objects.get(entity=self.books, code="1000").account_type
        ikeja_gl = Account.objects.create(entity=self.books, code="11811", name="Ikeja Collections",
                                          account_type=cash_type, is_postable=True)
        bank = BankAccount.objects.create(entity=self.books, name="Ikeja Collections",
                                          branch=self.ikeja, gl_account=ikeja_gl)
        hq = self.client_for("pick-gl@corona.test", "finance.customer.view")

        rows = {row["code"]: row for row in self.rows(hq, "accounts/?with_tags=true")}

        self.assertEqual(rows["11811"]["bank_account_id"], bank.pk)
        self.assertEqual(rows["11811"]["bank_branch_id"], self.ikeja.pk)
        self.assertIsNone(rows["4100"]["bank_account_id"])
        self.assertIsNone(rows["4100"]["bank_branch_id"])


class PaymentsPickersOfferOnlyWhatTheClerkMayUseTests(_PickerFixture):
    """``?own=true`` lists what a branch clerk may raise a gateway record against."""

    def setUp(self):
        from vs_procurement.models import Vendor

        super().setUp()
        payable = Account.objects.get(entity=self.books, code="2100")
        self.vendors = {
            code: Vendor.objects.create(entity=self.books, code=code, name=f"Vendor {code}",
                                        branch=branch, payable_account=payable)
            for code, branch in (("VIKJ", self.ikeja), ("VLEK", self.lekki), ("VALL", None))
        }

    def vendor_rows(self, client, query=""):
        response = client.get(f"/v1/procurement/vendors/?entity={self.books.code}{query}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["code"]: row for row in response.json()["data"]}

    def test_a_branch_clerks_own_customers_leave_out_the_shared_family(self):
        clerk = self.client_for("pick-clerk@corona.test", "finance.customer.view", branches=[self.ikeja])

        self.assertEqual({r["code"] for r in self.rows(clerk, "customers/")}, {"COKAF", "CADEY"})
        self.assertEqual({r["code"] for r in self.rows(clerk, "customers/?own=true")}, {"COKAF"})

    def test_a_whole_school_clerk_may_use_every_customer(self):
        hq = self.client_for("pick-own-hq@corona.test", "finance.customer.view")

        self.assertEqual({r["code"] for r in self.rows(hq, "customers/?own=true")}, {"COKAF", "CADEY"})

    def test_vendor_rows_carry_their_branch_and_own_leaves_out_the_shared_ones(self):
        clerk = self.client_for("pick-buyer@corona.test", "procurement.vendor.view", branches=[self.ikeja])

        rows = self.vendor_rows(clerk)
        self.assertEqual(set(rows), {"VIKJ", "VALL"})
        self.assertEqual(rows["VIKJ"]["branch_id"], self.ikeja.pk)
        self.assertIsNone(rows["VALL"]["branch_id"])
        self.assertEqual(set(self.vendor_rows(clerk, "&own=true")), {"VIKJ"})
