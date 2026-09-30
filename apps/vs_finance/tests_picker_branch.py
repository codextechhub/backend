"""Pickers and forms can tell whose a row is.

Corona has Ikeja, Lekki and Yaba. The Okafor family is filed under Ikeja and the
Adeyemi family is shared by every branch. A form raising a document against the
Okafors takes Ikeja without asking, and one against the Adeyemis asks which
branch; a deposit picker on an Ikeja receipt offers Ikeja's collection ledger, not
Lekki's; a branch clerk's payments pickers offer only what they may use.
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


class ACustomerNamesItsOwnBranchTests(_PickerFixture):
    """A reader covering several branches files a new customer under one of theirs."""

    def create(self, client, **body):
        return client.post(f"/v1/finance/customers/?entity={self.books.code}", {
            "name": "Parent New", "billing_email": "parent@example.com",
            "billing_phone": "08030000000", **body,
        }, format="json")

    def test_a_two_branch_reader_names_one_of_their_branches(self):
        both = self.client_for("cust-both@corona.test", "finance.customer.create",
                               branches=[self.ikeja, self.lekki])

        response = self.create(both, branch=self.lekki.pk)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Customer.objects.get(code=response.data["data"]["code"]).branch_id, self.lekki.pk)

    def test_they_may_not_name_a_branch_they_do_not_work_in(self):
        both = self.client_for("cust-both2@corona.test", "finance.customer.create",
                               branches=[self.ikeja, self.lekki])

        self.assertEqual(self.create(both, branch=self.yaba.pk).status_code, 403)

    def test_naming_none_asks_them_which(self):
        both = self.client_for("cust-both3@corona.test", "finance.customer.create",
                               branches=[self.ikeja, self.lekki])

        self.assertEqual(self.create(both).status_code, 400)

    def test_a_whole_school_reader_may_leave_it_shared(self):
        hq = self.client_for("cust-hq@corona.test", "finance.customer.create")

        response = self.create(hq)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(Customer.objects.get(code=response.data["data"]["code"]).branch_id)


class InvoiceAndPlanRowsCarryTheirOwnBranchTests(_PickerFixture):
    """A payment form narrows its deposit picker by the document's branch.

    Ikeja raises an invoice for the Adeyemis, whom every branch shares, and
    spreads it over a payment plan. The customer names no branch, so a form
    reading the customer's would offer Lekki's bank account for the payment and
    meet the server's refusal. The invoice and the plan name Ikeja themselves.
    """

    def setUp(self):
        from vs_finance.models import PaymentPlan

        super().setUp()
        self.bill = self.invoice(self.books, self.adeyemi, self.ikeja)
        self.plan = PaymentPlan.objects.create(
            entity=self.books, branch=self.ikeja, customer=self.adeyemi, invoice=self.bill,
            start_date=self.bill.invoice_date,
        )
        self.hq = self.client_for("pick-doc@corona.test", "finance.invoice.view",
                                  "finance.paymentplan.view")

    def detail(self, path):
        response = self.hq.get(f"/v1/finance/{path}?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.json()["data"]

    def test_an_ikeja_invoice_for_a_shared_customer_reads_ikeja(self):
        rows = {row["id"]: row for row in self.rows(self.hq, "invoices/")}

        self.assertEqual((rows[self.bill.pk]["branch_id"], rows[self.bill.pk]["branch_name"]),
                         (self.ikeja.pk, "Ikeja Branch"))
        self.assertEqual(self.detail(f"invoices/{self.bill.pk}/")["invoice"]["branch_id"],
                         self.ikeja.pk)

    def test_its_payment_plan_reads_ikeja_too(self):
        rows = {row["id"]: row for row in self.rows(self.hq, "payment-plans/")}

        self.assertEqual((rows[self.plan.pk]["branch_id"], rows[self.plan.pk]["branch_name"]),
                         (self.ikeja.pk, "Ikeja Branch"))
        self.assertEqual(self.detail(f"payment-plans/{self.plan.pk}/")["branch_id"], self.ikeja.pk)

    def test_the_branch_costs_no_extra_query_per_row(self):
        """Three rows from three branches cost what one row costs.

        Measured against a one-row list rather than a fixed number, so the
        assertion cannot drift when unrelated middleware adds a query of its own.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from vs_finance.models import PaymentPlan

        for path in ("invoices/", "payment-plans/"):
            self.rows(self.hq, path)
        with CaptureQueriesContext(connection) as invoices_once:
            self.rows(self.hq, "invoices/")
        with CaptureQueriesContext(connection) as plans_once:
            self.rows(self.hq, "payment-plans/")

        for branch in (self.lekki, self.yaba):
            bill = self.invoice(self.books, self.adeyemi, branch)
            PaymentPlan.objects.create(entity=self.books, branch=branch, customer=self.adeyemi,
                                       invoice=bill, start_date=bill.invoice_date)

        with self.assertNumQueries(len(invoices_once)):
            self.assertEqual(len(self.rows(self.hq, "invoices/")), 3)
        with self.assertNumQueries(len(plans_once)):
            self.assertEqual(len(self.rows(self.hq, "payment-plans/")), 3)
