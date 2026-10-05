"""Branch filters, the payer count and the release list on the receivables screens.

Corona runs Ikeja and Lekki; Harbour runs one branch. The Eze family is billed
at Ikeja, the Bellos at Lekki, and the Adebayo Trust, filed under no branch,
pays for both.

* ``?branch=`` narrows the deferred income summary, the provision runs, the
  deposits, the credit transfers and the payments from a payer to one branch
  the caller works in. Mrs Bello keeps Lekki's books: asking for Ikeja is
  answered exactly like asking for a branch that does not exist.
* ``pays_for_count`` on the customer list says how many customers a payer pays
  for, counting only the ones the reader can see, at the same cost for one row
  as for many.
* ``deferred-income/releases/`` lists each release journal with its branch,
  month, period, amount, journal and whether it was reversed, so the undo form
  offers real releases.
* A bank account carries its provider settlement route only for a caller who
  reaches the whole school or reads the payment settings.
"""
from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.test_utils import TenantAPIClient
from vs_finance.constants import ChargeKind, DocumentStatus
from vs_finance.models import BankAccount, CustomerCreditTransfer

from .tests_accruals import D, _AccrualFixture
from .tests_branch_scope import _FinanceBranchFixture
from .tests_payer_payments import JAN_15, _PayerFixture

BRANCH_NOT_FOUND_CODE = 400

KEYS = (
    "finance.deferredincome.view", "finance.provision.view", "finance.deposit.view",
    "finance.credittransfer.view", "finance.customer.view",
)


class _ReaderFixture(_AccrualFixture):
    """Mrs Okafor reads every branch, Mrs Bello Lekki alone, the Harbour bursar her one branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        grant, user_for = _FinanceBranchFixture.grant, _FinanceBranchFixture.user_for
        cls.okafor = grant(user_for(cls.tenant, "okafor@filters.test"), *KEYS,
                           tenant=cls.tenant, role_key="filters-whole")
        cls.bello_user = grant(user_for(cls.tenant, "bello@filters.test"), *KEYS,
                               tenant=cls.tenant, role_key="filters-lekki", branch=cls.lekki)
        cls.harbour_user = grant(user_for(cls.solo_tenant, "bursar@filters.test"), *KEYS,
                                 tenant=cls.solo_tenant, role_key="filters-harbour")
        cls.no_keys = user_for(cls.tenant, "nokeys@filters.test")

    def setUp(self):
        super().setUp()
        self.whole = TenantAPIClient(user=self.okafor)
        self.lekki_bursar = TenantAPIClient(user=self.bello_user)
        self.harbour = TenantAPIClient(user=self.harbour_user)

    def get(self, client, path, books=None, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        return client.get(f"/v1/finance/{path}?entity={(books or self.books).code}{query}")

    def assert_unknown_branch(self, response):
        self.assertEqual(response.status_code, BRANCH_NOT_FOUND_CODE, response.data)
        self.assertIn("branch", str(response.data))


class BranchParameterTests(_ReaderFixture):
    """Each list keeps one branch's rows, and refuses a branch outside the caller's reach."""

    def test_deferred_income_is_summed_for_the_branch_asked_for(self):
        self.bill(self.eze)
        self.bill(self.bello, 90_000)

        ikeja = self.get(self.whole, "deferred-income/", branch=self.ikeja.pk)
        lekki = self.get(self.lekki_bursar, "deferred-income/", branch=self.lekki.pk)

        self.assertEqual(ikeja.data["data"]["pending"], 150_000)
        self.assertEqual(lekki.data["data"]["pending"], 90_000)
        self.assert_unknown_branch(self.get(self.lekki_bursar, "deferred-income/",
                                            branch=self.ikeja.pk))

    def test_provision_runs_are_kept_where_they_have_a_line_for_the_branch(self):
        from vs_finance.provisions import prepare_provision

        self.bill(self.eze, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        run = prepare_provision(self.books, as_of=D(2026, 12, 31))

        ikeja = self.get(self.whole, "provisions/", branch=self.ikeja.pk)
        lekki = self.get(self.whole, "provisions/", branch=self.lekki.pk)

        self.assertEqual([row["id"] for row in ikeja.data["data"]], [run.pk])
        self.assertEqual(lekki.data["data"], [])

    def test_deposits_are_kept_for_the_branch_asked_for(self):
        for customer in (self.eze, self.bello):
            self.bill(customer, 50_000, on=D(2026, 1, 10), service=None, kind=ChargeKind.DEPOSIT)

        lekki = self.get(self.whole, "deposits/", branch=self.lekki.pk)

        self.assertEqual([row["customer_code"] for row in lekki.data["data"]], ["BELLO"])
        self.assert_unknown_branch(self.get(self.lekki_bursar, "deposits/", branch=self.ikeja.pk))

    def test_credit_transfers_are_kept_for_the_branch_asked_for(self):
        for branch, source, target in ((self.ikeja, self.eze, self.trust),
                                       (self.lekki, self.bello, self.trust)):
            CustomerCreditTransfer.objects.create(
                entity=self.books, branch=branch, from_customer=source, to_customer=target,
                transfer_date=D(2026, 1, 15), amount=10_000, status=DocumentStatus.DRAFT)

        ikeja = self.get(self.whole, "credit-transfers/", branch=self.ikeja.pk)

        self.assertEqual([row["from_customer_code"] for row in ikeja.data["data"]], ["EZE"])
        self.assert_unknown_branch(self.get(self.lekki_bursar, "credit-transfers/",
                                            branch=self.ikeja.pk))

    def test_a_branch_that_does_not_exist_reads_the_same_as_one_out_of_reach(self):
        unknown = self.get(self.lekki_bursar, "deposits/", branch=999_999)
        out_of_reach = self.get(self.lekki_bursar, "deposits/", branch=self.ikeja.pk)

        self.assertEqual((unknown.status_code, unknown.data),
                         (out_of_reach.status_code, out_of_reach.data))

    def test_a_school_with_one_branch_filters_by_it(self):
        self.bill(self.ada)

        response = self.get(self.harbour, "deferred-income/", self.solo_books,
                            branch=self.solo_main.pk)

        self.assertEqual(response.data["data"]["pending"], 150_000)

    def test_another_schools_branch_is_unknown_here(self):
        self.assert_unknown_branch(self.get(self.whole, "deposits/", branch=self.solo_main.pk))


class DeferredIncomeReleaseListTests(_ReaderFixture):
    """The releases the undo form offers: real journals, per branch and month."""

    @classmethod
    def setUpTestData(cls):
        from vs_finance.deferred_income import release_deferred_income

        super().setUpTestData()
        cls.bill_for(cls.eze)
        cls.bill_for(cls.bello)
        release_deferred_income(cls.books, up_to=D(2027, 2, 28))

    @classmethod
    def bill_for(cls, customer):
        from vs_finance.models import Account, Invoice, InvoiceLine
        from vs_finance.receivables import post_invoice

        from .tests_accruals import SECOND_TERM

        invoice = Invoice.objects.create(
            entity=cls.books, customer=customer, branch=customer.branch,
            invoice_date=D(2026, 12, 10), due_date=D(2026, 12, 25))
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=150_000,
            revenue_account=Account.objects.get(entity=cls.books, code="4100"),
            service_start=SECOND_TERM[0], service_end=SECOND_TERM[1])
        post_invoice(invoice)

    def releases(self, client, **params):
        response = self.get(client, "deferred-income/releases/", **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_each_release_names_its_branch_month_period_and_journal(self):
        rows = self.releases(self.whole)

        self.assertEqual(
            sorted((r["branch_name"], r["month"], r["amount"]) for r in rows),
            [("Ikeja Branch", "2027-01", 37_500), ("Ikeja Branch", "2027-02", 37_500),
             ("Lekki Branch", "2027-01", 37_500), ("Lekki Branch", "2027-02", 37_500)],
        )
        january = next(r for r in rows if r["month"] == "2027-01")
        period = self.period(2027, 1)
        self.assertEqual((january["period_id"], january["period_status"], january["date"]),
                         (period.pk, "OPEN", "2027-01-31"))
        self.assertTrue(january["journal_id"])
        self.assertEqual((january["reversed"], january["can_reverse"]), (False, True))

    def test_a_reversed_release_says_so_and_cannot_be_offered_again(self):
        from vs_finance.deferred_income import reverse_deferred_release

        reverse_deferred_release(self.books, self.period(2027, 1))

        reversed_rows = self.releases(self.whole, reversed="true")
        live_rows = self.releases(self.whole, reversed="false")

        self.assertEqual({r["month"] for r in reversed_rows}, {"2027-01"})
        self.assertTrue(all(r["reversed"] and not r["can_reverse"] for r in reversed_rows))
        self.assertEqual({r["month"] for r in live_rows}, {"2027-02"})

    def test_filters_by_period_month_and_branch(self):
        by_period = self.releases(self.whole, period=self.period(2027, 2).pk)
        by_month = self.releases(self.whole, month="2027-02", branch=self.lekki.pk)

        self.assertEqual({r["month"] for r in by_period}, {"2027-02"})
        self.assertEqual([(r["branch_id"], r["month"]) for r in by_month],
                         [(self.lekki.pk, "2027-02")])

    def test_a_lekki_bursar_sees_lekkis_releases_only(self):
        rows = self.releases(self.lekki_bursar)

        self.assertEqual({r["branch_id"] for r in rows}, {self.lekki.pk})
        self.assert_unknown_branch(self.get(self.lekki_bursar, "deferred-income/releases/",
                                            branch=self.ikeja.pk))

    def test_a_caller_without_the_key_or_from_another_school_reads_nothing(self):
        refused = self.get(TenantAPIClient(user=self.no_keys), "deferred-income/releases/")
        rival = self.get(self.harbour, "deferred-income/releases/")

        self.assertEqual(refused.status_code, 403)
        self.assertIn(rival.status_code, (403, 404))

    def test_a_school_with_one_branch_lists_its_releases(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.ada)
        release_deferred_income(self.solo_books, up_to=D(2027, 1, 31))

        rows = self.releases(self.harbour, books=self.solo_books)

        self.assertEqual([(r["branch_id"], r["month"]) for r in rows],
                         [(self.solo_main.pk, "2027-01")])


class PaysForCountTests(_ReaderFixture):
    """The Trust pays for Eze at Ikeja and Bello at Lekki."""

    @classmethod
    def setUpTestData(cls):
        from vs_finance.payer_payments import link_customer

        super().setUpTestData()
        link_customer(cls.trust, cls.eze)
        link_customer(cls.trust, cls.bello)

    def counts(self, client):
        response = self.get(client, "customers/")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["code"]: row["pays_for_count"] for row in response.data["data"]}

    def test_a_whole_school_reader_counts_both_and_a_lekki_bursar_counts_bello(self):
        self.assertEqual(self.counts(self.whole), {"BELLO": 0, "EZE": 0, "TRUST": 2})
        self.assertEqual(self.counts(self.lekki_bursar)["TRUST"], 1)

    def test_an_ended_link_is_not_counted(self):
        from vs_finance.models import PayerLink

        PayerLink.objects.filter(payer=self.trust, customer=self.bello).update(is_active=False)

        self.assertEqual(self.counts(self.whole)["TRUST"], 1)

    def test_the_count_costs_one_query_however_many_payers_are_listed(self):
        from vs_finance.payer_payments import link_customer

        self.counts(self.whole)
        with CaptureQueriesContext(connection) as before:
            self.counts(self.whole)
        for code in ("PAYER1", "PAYER2", "PAYER3"):
            payer = self.customer(self.books, code, None)
            link_customer(payer, self.eze)
        with CaptureQueriesContext(connection) as after:
            counts = self.counts(self.whole)

        self.assertEqual(counts["PAYER3"], 1)
        self.assertEqual(len(after), len(before), [q["sql"] for q in after.captured_queries])


class PayerPaymentBranchParameterTests(_PayerFixture):
    """N450,000 into Ikeja's bank, with Chidi's share held there for Lekki."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = ("finance.payment.view", "finance.bankaccount.view")
        cls.whole_user = cls.grant(cls.user_for(cls.tenant, "pp-whole@corona.test"), *keys,
                                   tenant=cls.tenant, role_key="pp-whole")
        cls.ikeja_user = cls.grant(cls.user_for(cls.tenant, "pp-ikeja@corona.test"), *keys,
                                   tenant=cls.tenant, role_key="pp-ikeja", branch=cls.ikeja)
        cls.solo_reader = cls.grant(cls.user_for(cls.solo_tenant, "pp-solo@solo.test"), *keys,
                                    tenant=cls.solo_tenant, role_key="pp-solo")
        from vs_finance.payer_payments import record_payer_payment

        cls.document = record_payer_payment(
            cls.okafor, bank_account=cls.ikeja_bank, amount=450_000_00, payment_date=JAN_15)

    def ids(self, client, **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        response = client.get(self.url("payer-payments/") + query)
        return response.status_code, [row["id"] for row in response.data.get("data") or []]

    def test_the_payment_is_listed_for_the_branch_it_reached_and_the_one_it_holds_for(self):
        whole = TenantAPIClient(user=self.whole_user)

        self.assertEqual(self.ids(whole, branch=self.ikeja.pk), (200, [self.document.pk]))
        self.assertEqual(self.ids(whole, branch=self.lekki.pk), (200, [self.document.pk]))
        self.assertEqual(self.ids(whole, branch=self.yaba.pk), (200, []))

    def test_an_ikeja_bursar_naming_lekki_is_told_it_does_not_exist(self):
        status, _ = self.ids(TenantAPIClient(user=self.ikeja_user), branch=self.lekki.pk)

        self.assertEqual(status, BRANCH_NOT_FOUND_CODE)


class BankAccountSettlementRouteTests(_PayerFixture):
    """Lekki's account settles through a Paystack subaccount; who may read that route."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        BankAccount.objects.filter(pk=cls.lekki_bank.pk).update(
            gateway_subaccount_code="ACCT_LEK", gateway_subaccount_provider="PAYSTACK",
            settlement_bank_code="044")
        cls.whole_user = cls.grant(cls.user_for(cls.tenant, "route-whole@corona.test"),
                                   "finance.bankaccount.view", tenant=cls.tenant,
                                   role_key="route-whole")
        cls.lekki_user = cls.grant(cls.user_for(cls.tenant, "route-lekki@corona.test"),
                                   "finance.bankaccount.view", tenant=cls.tenant,
                                   role_key="route-lekki", branch=cls.lekki)
        cls.lekki_settings_user = cls.grant(
            cls.user_for(cls.tenant, "route-lekki-pay@corona.test"),
            "finance.bankaccount.view", "payments.settings.view", tenant=cls.tenant,
            role_key="route-lekki-pay", branch=cls.lekki)

    def lekki_row(self, user):
        response = TenantAPIClient(user=user).get(self.url("bank-accounts/"))
        self.assertEqual(response.status_code, 200, response.data)
        return next(row for row in response.data["data"] if row["id"] == self.lekki_bank.pk)

    def test_a_whole_school_reader_sees_the_route(self):
        row = self.lekki_row(self.whole_user)

        self.assertEqual(
            (row["gateway_subaccount_code"], row["gateway_subaccount_provider"],
             row["settlement_bank_code"]),
            ("ACCT_LEK", "PAYSTACK", "044"))

    def test_a_lekki_bursar_without_the_payment_settings_key_does_not(self):
        row = self.lekki_row(self.lekki_user)

        for name in ("gateway_subaccount_code", "gateway_subaccount_provider",
                     "settlement_bank_code"):
            self.assertNotIn(name, row)

    def test_a_lekki_bursar_who_reads_the_payment_settings_does(self):
        self.assertEqual(self.lekki_row(self.lekki_settings_user)["gateway_subaccount_code"],
                         "ACCT_LEK")

