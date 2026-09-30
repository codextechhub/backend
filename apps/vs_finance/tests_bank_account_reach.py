"""A bank account named in a posting must be one the caller can see.

Corona runs Ikeja, Lekki and Yaba, and keeps a collection account at each
branch, plus a GTBank operations account opened before accounts carried a
branch. Ikeja's bursar sees Ikeja's account only: a bank account holds one
branch's money, and one not yet given a branch is nobody's to her. Every route
that moves money out of a named account (refunds singly and in batches,
expense-claim reimbursements, tax payments, asset purchases and sales, petty-cash
floats and top-ups, payroll runs and their payment, vendor payments) resolves
that account through one resolver, so typing Lekki's account id, or the
unbranched one's, into any of them answers the same 404 as a mistyped id and
writes nothing.

Each family is asserted separately because the defect was never one route: it
was the resolver they share reading the whole school's books.
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, BankAccount

from .tests_branch_scope import _FinanceBranchFixture

JAN = datetime.date(2026, 1, 12)
_bursars = itertools.count(1)


class BankAccountNamedInAPostingTests(_FinanceBranchFixture):
    """Each money-out route refuses every account but the caller's own branch's."""

    def setUp(self):
        super().setUp()
        self.ikeja_bank = self.bank("Ikeja Collections", self.ikeja, "30")
        self.lekki_bank = self.bank("Lekki Collections", self.lekki, "31")
        self.shared_bank = self.bank("GTBank Operations", None, "32")

    def bank(self, name, branch, tag):
        gl = Account.objects.create(
            entity=self.books, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=self.books, code="1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(
            entity=self.books, name=name, branch=branch, gl_account=gl,
        )

    def bursar(self, *keys):
        """Ikeja's bursar, pinned to Ikeja, holding ``keys``."""
        n = next(_bursars)
        return TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, f"bursar-{n}@corona.test"), *keys,
            tenant=self.tenant, role_key=f"bursar-{n}", branch=self.ikeja,
        ))

    def post(self, client, path, body):
        return client.post(
            f"/v1/{path}?entity={self.books.code}", body, format="json",
        )

    def assertRefusedAsUnknown(self, response):
        self.assertEqual(response.status_code, 404, response.data)
        self.assertIn("No bank account", str(response.data))

    def assertBankAccepted(self, response):
        """The resolver let the account through, whatever the posting then said."""
        self.assertNotIn("No bank account", str(response.data))

    def assertEachBank(self, client, path, body, *, refused_count=None):
        """Lekki's account and the unbranched one are unknown; Ikeja's resolves."""
        for bank in (self.lekki_bank, self.shared_bank):
            with self.subTest(path=path, refused=bank.name):
                response = self.post(client, path, {**body, "bank_account": bank.pk})
                self.assertRefusedAsUnknown(response)
                if refused_count is not None:
                    self.assertEqual(refused_count(), 0, f"{path} wrote a row before refusing")
        self.assertBankAccepted(
            self.post(client, path, {**body, "bank_account": self.ikeja_bank.pk}))

    # -- refunds ---------------------------------------------------------------- #

    def test_a_refund_names_only_a_reachable_account(self):
        from vs_finance.models import Refund

        customer = self.customer(self.books, "CREF", self.ikeja)
        self.assertEachBank(
            self.bursar("finance.refund.create"), "finance/refunds/",
            {"customer": customer.code, "amount": 1000, "refund_date": JAN.isoformat()},
            refused_count=Refund.objects.filter(customer=customer).count,
        )

    def test_a_refund_by_name_is_refused_the_same_way(self):
        customer = self.customer(self.books, "CREFN", self.ikeja)
        client = self.bursar("finance.refund.create")
        response = self.post(client, "finance/refunds/", {
            "customer": customer.code, "amount": 1000,
            "refund_date": JAN.isoformat(), "bank_account": self.lekki_bank.name,
        })
        self.assertRefusedAsUnknown(response)

    def test_a_refund_batch_names_only_a_reachable_account(self):
        from vs_finance.models import Refund

        customer = self.customer(self.books, "CBAT", self.ikeja)
        client = self.bursar("finance.refund.create", "finance.refund.post")
        response = self.post(client, "finance/ar-adjustments/batch/", {
            "kind": "REFUND", "action": "DRAFT", "date": JAN.isoformat(),
            "bank_account": self.lekki_bank.pk,
            "items": [{"customer": customer.code, "amount": 1000}],
        })
        self.assertRefusedAsUnknown(response)
        self.assertFalse(Refund.objects.filter(customer=customer).exists())

    # -- operations ------------------------------------------------------------- #

    def test_an_expense_claim_reimbursement(self):
        from vs_finance.models import ExpenseClaim

        claim = ExpenseClaim.objects.create(entity=self.books, branch=self.ikeja, claim_date=JAN)
        self.assertEachBank(
            self.bursar("finance.expenseclaim.settle"),
            f"finance/expense-claims/{claim.pk}/settle/",
            {"pay_date": JAN.isoformat()},
        )

    def test_a_tax_payment(self):
        from vs_finance.models import TaxFiling, TaxObligation

        filing = TaxFiling.objects.create(
            entity=self.books, branch=self.ikeja,
            obligation=TaxObligation.objects.filter(entity=self.books).first(),
            period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),
        )
        self.assertEachBank(
            self.bursar("finance.tax.pay"),
            f"finance/tax-filings/{filing.pk}/pay/",
            {"pay_date": JAN.isoformat()},
        )

    def test_an_asset_purchase_and_sale(self):
        asset = self.fixed_asset(self.books, "Ikeja Bus", self.ikeja)
        self.assertEachBank(
            self.bursar("finance.fixedasset.acquire"),
            f"finance/fixed-assets/{asset.pk}/acquire/", {},
        )
        sold = self.fixed_asset(self.books, "Ikeja Generator", self.ikeja)
        self.assertEachBank(
            self.bursar("finance.fixedasset.dispose"),
            f"finance/fixed-assets/{sold.pk}/dispose/",
            {"disposal_date": JAN.isoformat(), "proceeds": 1000},
        )

    def test_a_petty_cash_float_and_top_up(self):
        from vs_finance.models import PettyCashFund

        fund = PettyCashFund.objects.create(
            entity=self.books, branch=self.ikeja, name="Ikeja Float",
            gl_account=Account.objects.create(
                entity=self.books, code="1140", name="Ikeja Tin",
                account_type=Account.objects.get(entity=self.books, code="1000").account_type,
                is_postable=True,
            ),
        )
        self.assertEachBank(
            self.bursar("finance.pettycash.establish"),
            f"finance/petty-cash-funds/{fund.pk}/establish/",
            {"amount": 1000, "date": JAN.isoformat()},
        )
        self.assertEachBank(
            self.bursar("finance.pettycash.replenish"),
            f"finance/petty-cash-funds/{fund.pk}/replenish/",
            {"date": JAN.isoformat()},
        )

    def test_a_payroll_run_and_its_payment(self):
        from vs_finance.models import PayrollRun

        run = PayrollRun.objects.create(entity=self.books, branch=self.ikeja, pay_date=JAN)
        self.assertEachBank(
            self.bursar("finance.payrollrun.pay"),
            f"finance/payroll-runs/{run.pk}/pay/", {},
        )
        before = PayrollRun.objects.count()
        response = self.post(self.bursar("finance.payrollrun.create"), "finance/payroll-runs/", {
            "pay_date": "2026-02-27", "bank_account": self.lekki_bank.pk,
            "lines": [{"employee_name": "Ada Obi", "gross_amount": 100000}],
        })
        self.assertRefusedAsUnknown(response)
        self.assertEqual(PayrollRun.objects.count(), before)

    def test_a_vendor_payment(self):
        from vs_procurement.models import Vendor, VendorPayment

        vendor = Vendor.objects.create(
            entity=self.books, code="ACME", name="Acme Supplies",
            payable_account=Account.objects.get(entity=self.books, code="2100"),
            kyc_status="VERIFIED",
        )
        self.assertEachBank(
            self.bursar("procurement.vendor_payment.create"), "procurement/vendor-payments/",
            {"vendor": vendor.pk, "payment_date": JAN.isoformat()},
            refused_count=VendorPayment.objects.filter(vendor=vendor).count,
        )


class DocumentPaidFromItsOwnBranchTests(BankAccountNamedInAPostingTests):
    """A branch's document is paid from that branch's account and no other.

    Mrs Okafor is bursar at both Ikeja and Lekki, so Lekki's account is in her
    bank list. An Ikeja document paid from it would leave Ikeja owing and Lekki
    short, so it is refused with a 400 naming the branch, not hidden as a 404.
    The account not yet given a branch is outside her reach altogether (404).
    """

    MESSAGE = "belongs to Ikeja Branch. Pay it from an Ikeja Branch account."

    def bursar(self, *keys):
        """Mrs Okafor, bound to Ikeja and to Lekki."""
        n = next(_bursars)
        user = self.user_for(self.tenant, f"okafor-{n}@corona.test")
        for branch in (self.ikeja, self.lekki):
            self.grant(user, *keys, tenant=self.tenant,
                       role_key=f"okafor-{n}-{branch.pk}", branch=branch)
        return TenantAPIClient(user=user)

    def assertEachBank(self, client, path, body, *, refused_count=None, noun=None):
        response = self.post(client, path, {**body, "bank_account": self.lekki_bank.pk})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(self.MESSAGE, str(response.data))
        if refused_count is not None:
            self.assertEqual(refused_count(), 0, f"{path} wrote a row before refusing")
        self.assertRefusedAsUnknown(
            self.post(client, path, {**body, "bank_account": self.shared_bank.pk}))
        accepted = self.post(client, path, {**body, "bank_account": self.ikeja_bank.pk})
        self.assertNotIn("No bank account", str(accepted.data))
        self.assertNotIn("Pay it from", str(accepted.data))

    def test_a_refund_by_name_is_refused_the_same_way(self):
        customer = self.customer(self.books, "CREFN", self.ikeja)
        response = self.post(self.bursar("finance.refund.create"), "finance/refunds/", {
            "customer": customer.code, "amount": 1000,
            "refund_date": JAN.isoformat(), "bank_account": self.lekki_bank.name,
        })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("This refund " + self.MESSAGE, str(response.data))

    def test_a_refund_batch_names_only_a_reachable_account(self):
        from vs_finance.models import Refund

        customer = self.customer(self.books, "CBAT", self.ikeja)
        response = self.post(
            self.bursar("finance.refund.create", "finance.refund.post"),
            "finance/ar-adjustments/batch/", {
                "kind": "REFUND", "action": "DRAFT", "date": JAN.isoformat(),
                "bank_account": self.lekki_bank.pk,
                "items": [{"customer": customer.code, "amount": 1000}],
            })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(self.MESSAGE, str(response.data))
        self.assertFalse(Refund.objects.filter(customer=customer).exists())

    def test_a_payroll_run_and_its_payment(self):
        from vs_finance.models import PayrollRun

        run = PayrollRun.objects.create(entity=self.books, branch=self.ikeja, pay_date=JAN)
        self.assertEachBank(
            self.bursar("finance.payrollrun.pay"), f"finance/payroll-runs/{run.pk}/pay/", {},
        )

    def tax_return(self, *branches):
        """The school's January return, filed with no branch and a N500 share per branch."""
        from vs_finance.models import TaxFiling, TaxFilingShare, TaxObligation

        filing = TaxFiling.objects.create(
            entity=self.books,
            obligation=TaxObligation.objects.filter(entity=self.books).first(),
            period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),
            filing_status="FILED", filed_at=datetime.date(2026, 1, 5),
            gross_liability=50_000 * len(branches), amount_due=50_000 * len(branches),
        )
        for branch in branches:
            TaxFilingShare.objects.create(
                filing=filing, branch=branch, gross_liability=50_000, amount_due=50_000,
            )
        return filing

    def test_a_tax_payment(self):
        """Ikeja's share of the school's return is paid from Ikeja's account only.

        The return names no branch; she reaches it through its Ikeja share.
        """
        filing = self.tax_return(self.ikeja, self.yaba)
        self.assertEachBank(
            self.bursar("finance.tax.pay"), f"finance/tax-filings/{filing.pk}/pay/",
            {"pay_date": JAN.isoformat(), "branch": self.ikeja.pk},
        )
        self.assertEqual(filing.shares.get(branch=self.ikeja).amount_paid, 50_000)
        self.assertEqual(filing.shares.get(branch=self.yaba).amount_paid, 0)

    def test_a_tax_return_with_no_share_of_hers_is_not_found(self):
        """A return whose only share is Yaba's is outside her reach, so she may not pay it.

        She covers Ikeja and Lekki. Whichever account she names, her Ikeja one,
        her Lekki one or the unbranched one, the return is not found for her.
        """
        filing = self.tax_return(self.yaba)
        client = self.bursar("finance.tax.pay")
        for bank in (self.ikeja_bank, self.lekki_bank, self.shared_bank):
            with self.subTest(bank=bank.name):
                response = self.post(client, f"finance/tax-filings/{filing.pk}/pay/",
                                     {"pay_date": JAN.isoformat(), "bank_account": bank.pk})
                self.assertEqual(response.status_code, 404, response.data)
                filing.refresh_from_db()
                self.assertEqual(filing.amount_paid, 0)

    def test_a_vendor_payment(self):
        """Settling an Ikeja bill is Ikeja's money; checked once the bills resolve."""
        from vs_finance.constants import DocumentStatus
        from vs_procurement.constants import ProcApprovalState
        from vs_procurement.models import Vendor, VendorInvoice, VendorPayment

        vendor = Vendor.objects.create(
            entity=self.books, code="ACME", name="Acme Supplies",
            payable_account=Account.objects.get(entity=self.books, code="2100"),
            kyc_status="VERIFIED",
        )
        bill = VendorInvoice.objects.create(
            entity=self.books, vendor=vendor, branch=self.ikeja,
            invoice_date=JAN, due_date=JAN, total=10_000, subtotal=10_000,
            status=DocumentStatus.POSTED, approval_state=ProcApprovalState.APPROVED,
        )
        self.assertEachBank(
            self.bursar("procurement.vendor_payment.create"), "procurement/vendor-payments/",
            {"vendor": vendor.pk, "payment_date": JAN.isoformat(),
             "allocations": [{"vendor_invoice": bill.pk, "amount": 10_000}]},
            refused_count=VendorPayment.objects.filter(vendor=vendor).count,
        )

    def test_a_shared_customers_refund_names_its_branch_and_pays_from_it(self):
        """No school-wide refund, and so no school-wide escape from the bank rule."""
        customer = self.customer(self.books, "CALLR", None)
        client = self.bursar("finance.refund.create")
        body = {"customer": customer.code, "amount": 1000, "refund_date": JAN.isoformat()}

        unnamed = self.post(client, "finance/refunds/", {
            **body, "bank_account": self.lekki_bank.pk})
        self.assertEqual(unnamed.status_code, 400, unnamed.data)
        self.assertIn("branch", unnamed.data["error"]["detail"])

        crossed = self.post(client, "finance/refunds/", {
            **body, "branch": self.ikeja.pk, "bank_account": self.lekki_bank.pk})
        self.assertEqual(crossed.status_code, 400, crossed.data)
        self.assertIn("This refund " + self.MESSAGE, str(crossed.data))


class BranchOnThePickersTests(_FinanceBranchFixture):
    """The screens narrow their bank picker from branch ids the API returns."""

    bank = BankAccountNamedInAPostingTests.bank

    def setUp(self):
        super().setUp()
        self.ikeja_bank = self.bank("Ikeja Pick", self.ikeja, "50")
        self.shared_bank = self.bank("Shared Pick", None, "51")

    def test_bank_accounts_and_the_documents_that_pay_from_them_carry_their_branch(self):
        from vs_finance.models import ExpenseClaim

        claim = ExpenseClaim.objects.create(entity=self.books, branch=self.ikeja, claim_date=JAN)
        user = self.grant(
            self.user_for(self.tenant, "picker-hq@corona.test"),
            "finance.bankaccount.view", "finance.expenseclaim.view",
            tenant=self.tenant, role_key="picker-hq",
        )
        client = TenantAPIClient(user=user)
        banks = {
            row["id"]: row["branch_id"] for row in client.get(
                f"/v1/finance/bank-accounts/?entity={self.books.code}").data["data"]
        }
        self.assertEqual(banks[self.ikeja_bank.pk], self.ikeja.pk)
        self.assertIsNone(banks[self.shared_bank.pk])
        detail = client.get(f"/v1/finance/expense-claims/{claim.pk}/?entity={self.books.code}")
        self.assertEqual(detail.data["data"]["branch_id"], self.ikeja.pk)


class EligibleBillsNameTheirBranchTests(_FinanceBranchFixture):
    """The vendor-payment screen narrows its bank picker from the bills' branch."""

    def test_each_eligible_bill_carries_its_branch(self):
        from vs_finance.constants import DocumentStatus
        from vs_procurement.constants import ProcApprovalState
        from vs_procurement.models import Vendor, VendorInvoice

        vendor = Vendor.objects.create(
            entity=self.books, code="ACME", name="Acme Supplies",
            payable_account=Account.objects.get(entity=self.books, code="2100"),
            kyc_status="VERIFIED",
        )
        bill = VendorInvoice.objects.create(
            entity=self.books, vendor=vendor, branch=self.ikeja,
            invoice_date=JAN, due_date=JAN, total=10_000, subtotal=10_000,
            status=DocumentStatus.POSTED, approval_state=ProcApprovalState.APPROVED,
        )
        user = self.grant(self.user_for(self.tenant, "eligible-hq@corona.test"),
                          "procurement.vendor_payment.create", "procurement.vendor_payment.view",
                          tenant=self.tenant, role_key="eligible-hq")
        rows = TenantAPIClient(user=user).get(
            f"/v1/procurement/vendor-payments/eligible-invoices/?entity={self.books.code}"
            f"&vendor={vendor.pk}").data["data"]
        self.assertEqual({row["id"]: row["branch_id"] for row in rows}, {bill.pk: self.ikeja.pk})


class OneBranchSchoolBankTests(_FinanceBranchFixture):
    """At a school with one branch, an account opened before accounts carried a branch is that branch's."""

    def test_a_main_document_is_paid_from_the_unbranched_account(self):
        from vs_finance.models import ExpenseClaim

        gl = Account.objects.create(
            entity=self.solo_books, code="1133", name="Old Operations",
            account_type=Account.objects.get(entity=self.solo_books, code="1000").account_type,
            is_postable=True,
        )
        legacy = BankAccount.objects.create(
            entity=self.solo_books, name="Old Operations", branch=None, gl_account=gl,
        )
        claim = ExpenseClaim.objects.create(
            entity=self.solo_books, branch=self.solo_main, claim_date=JAN,
        )
        client = TenantAPIClient(user=self.grant(
            self.user_for(self.solo_tenant, "solo-bank@corona.test"),
            "finance.expenseclaim.settle", tenant=self.solo_tenant, role_key="solo-bank",
            branch=self.solo_main,
        ))

        response = client.post(
            f"/v1/finance/expense-claims/{claim.pk}/settle/?entity={self.solo_books.code}",
            {"pay_date": JAN.isoformat(), "bank_account": legacy.pk}, format="json",
        )

        self.assertNotIn("No bank account", str(response.data))
        self.assertNotIn("Pay it from", str(response.data))


class StatementImportBatchesFollowTheirAccountTests(BankAccountNamedInAPostingTests):
    """A bank-statement import is read by its bank account's branch, exclusively.

    Ikeja's bursar sees the statement imported into Ikeja's account, and neither
    Lekki's nor the one imported into the GTBank account nobody has given a
    branch. A student roll uploaded for the whole school stays visible to her: an
    ordinary import with no branch is still the school's.
    """

    def setUp(self):
        super().setUp()
        from vs_import_data.models import ImportBatch

        self.uploader = self.user_for(self.tenant, "uploader@corona.test")
        self.batches = {
            name: self.statement_batch(bank) for name, bank in (
                ("ikeja", self.ikeja_bank), ("lekki", self.lekki_bank), ("shared", self.shared_bank),
            )
        }
        self.roll = ImportBatch.all_objects.create(
            tenant=self.tenant, branch=None, uploaded_by=self.uploader,
            dataset_type="students", file="imports/roll.csv",
        )

    def statement_batch(self, bank):
        from vs_finance.models import BankStatementImportContext
        from vs_import_data.models import ImportBatch

        batch = ImportBatch.all_objects.create(
            tenant=self.tenant, branch=bank.branch, uploaded_by=self.uploader,
            dataset_type="bank_statements", file=f"imports/{bank.pk}.csv",
        )
        BankStatementImportContext.objects.create(
            import_batch=batch, bank_account=bank, statement_date=JAN,
            opening_balance=0, closing_balance=0, source_file_hash=f"{bank.pk:064d}",
        )
        return batch

    def visible(self, user):
        from types import SimpleNamespace

        from vs_import_data.models import ImportBatch
        from vs_import_data.scoping import batch_branch_q

        request = SimpleNamespace(user=user)
        return set(ImportBatch.all_objects.filter(
            batch_branch_q(request), tenant=self.tenant).values_list("pk", flat=True))

    def test_a_branch_bursar_reads_only_her_accounts_statement_imports(self):
        ikeja = self.grant(self.user_for(self.tenant, "imp-ikeja@corona.test"),
                           "finance.bankaccount.import", tenant=self.tenant,
                           role_key="imp-ikeja", branch=self.ikeja)

        self.assertEqual(self.visible(ikeja), {self.batches["ikeja"].pk, self.roll.pk})

    def test_a_statement_follows_its_account_once_the_account_is_placed(self):
        ikeja = self.grant(self.user_for(self.tenant, "imp-placed@corona.test"),
                           "finance.bankaccount.import", tenant=self.tenant,
                           role_key="imp-placed", branch=self.ikeja)
        BankAccount.objects.filter(pk=self.shared_bank.pk).update(branch=self.ikeja)

        self.assertIn(self.batches["shared"].pk, self.visible(ikeja))

    def test_a_whole_school_bursar_reads_every_import(self):
        head = self.grant(self.user_for(self.tenant, "imp-head@corona.test"),
                          "finance.bankaccount.import", tenant=self.tenant, role_key="imp-head")

        self.assertEqual(
            self.visible(head), {b.pk for b in self.batches.values()} | {self.roll.pk})
