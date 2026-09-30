"""A bank's ledger account is that bank's money under another name.

Corona keeps a collection account at Ikeja and at Lekki, and a GTBank operations
account opened before bank accounts carried a branch, each backed by its own
ledger account. Ikeja's bursar cannot name Lekki's bank account, or the
unbranched one, on a payment, so they must not be able to name their ledgers by
code either: as a receipt's deposit account, an
asset's credit account, a bank adjustment's counter account, a direct entry's
line, a payout's source, a vendor's account, or by editing the ledger account
itself. Each is refused exactly as an unknown account is. A school default they
cannot point anywhere, because a setting that binds every branch is changed only
by a caller whose reach is the whole school. A ledger account behind no bank
account is untouched, and a caller bound to no branch names any of them.
"""
from __future__ import annotations

import datetime
import itertools
import types

from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, BankAccount, JournalEntry, Payment

from .tests_branch_scope import _FinanceBranchFixture

JAN = datetime.date(2026, 1, 12)
_people = itertools.count(1)


class _LedgerReachFixture(_FinanceBranchFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_bank = cls.bank("Ikeja Collections", cls.ikeja, "40")
        cls.lekki_bank = cls.bank("Lekki Collections", cls.lekki, "41")
        cls.shared_bank = cls.bank("GTBank Operations", None, "42")
        cls.lekki_ledger = cls.lekki_bank.gl_account

    @classmethod
    def bank(cls, name, branch, tag):
        gl = Account.objects.create(
            entity=cls.books, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=cls.books, code="1000").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(entity=cls.books, name=name, branch=branch, gl_account=gl)

    def person(self, *keys, branch):
        n = next(_people)
        return self.grant(
            self.user_for(self.tenant, f"ledger-{n}@corona.test"), *keys,
            tenant=self.tenant, role_key=f"ledger-{n}", branch=branch,
        )

    def bursar(self, *keys):
        """Ikeja's bursar, pinned to Ikeja."""
        return TenantAPIClient(user=self.person(*keys, branch=self.ikeja))

    def post(self, client, path, body, **extra):
        return client.post(f"/v1/{path}?entity={self.books.code}", body, format="json", **extra)

    def assertRefusedAsUnknown(self, response):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(f"No account '{self.lekki_ledger.code}'", str(response.data))


class ResolverTests(_LedgerReachFixture):
    """The one resolver every write names a ledger account through."""

    def resolve(self, user, ref):
        from vs_finance.views_ops.base import _resolve_account

        return _resolve_account(types.SimpleNamespace(user=user), self.books, ref, "account")

    def test_another_branchs_bank_ledger_is_unknown_by_code_and_by_id(self):
        ikeja = self.person("finance.payment.create", branch=self.ikeja)
        for ref in (self.lekki_ledger.code, str(self.lekki_ledger.pk)):
            with self.subTest(ref=ref), self.assertRaises(ValidationError) as refused:
                self.resolve(ikeja, ref)
            self.assertEqual(
                str(refused.exception.detail["account"]), f"No account '{ref}' in this entity.")

    def test_own_and_plain_ledgers_resolve(self):
        ikeja = self.person("finance.payment.create", branch=self.ikeja)
        for account in (self.ikeja_bank.gl_account,
                        Account.objects.get(entity=self.books, code="4100")):
            with self.subTest(code=account.code):
                self.assertEqual(self.resolve(ikeja, account.code), account)

    def test_the_ledger_of_a_bank_not_yet_given_a_branch_is_unknown_to_them(self):
        ikeja = self.person("finance.payment.create", branch=self.ikeja)
        with self.assertRaises(ValidationError):
            self.resolve(ikeja, self.shared_bank.gl_account.code)

    def test_a_caller_bound_to_no_branch_names_any_bank_ledger(self):
        hq = self.person("finance.payment.create", branch=None)
        self.assertEqual(self.resolve(hq, self.lekki_ledger.code), self.lekki_ledger)

    def test_the_payments_lookup_refuses_to_resolve_a_ledger_account(self):
        from vs_payments.views import _entity_obj

        with self.assertRaises(TypeError):
            _entity_obj(None, self.books, Account, self.lekki_ledger.code, "source_account")


class LedgerNamedOnAWriteTests(_LedgerReachFixture):
    """Each write that names a ledger account refuses Lekki's bank ledger."""

    def test_a_receipt_deposited_into_it(self):
        customer = self.customer(self.books, "CRCPT", self.ikeja)
        client = self.bursar("finance.payment.create")
        response = self.post(client, f"finance/customers/{customer.code}/receipt/", {
            "amount": 5_000, "payment_date": JAN.isoformat(),
            "deposit_account": self.lekki_ledger.code,
        })
        self.assertRefusedAsUnknown(response)
        self.assertFalse(Payment.objects.filter(customer=customer).exists())

    def test_a_direct_entry_line(self):
        before = JournalEntry.objects.filter(entity=self.books).count()
        response = self.post(self.bursar("finance.directentry.post"), "finance/direct-entries/", {
            "date": JAN.isoformat(), "narration": "Grant",
            "lines": [
                {"account": self.lekki_ledger.code, "debit": 0, "credit": 5_000},
                {"account": "4100", "debit": 5_000, "credit": 0},
            ],
        })
        self.assertRefusedAsUnknown(response)
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), before)

    def test_an_asset_bought_on_it(self):
        asset = self.fixed_asset(self.books, "Ikeja Bus", self.ikeja)
        response = self.post(
            self.bursar("finance.fixedasset.acquire"),
            f"finance/fixed-assets/{asset.pk}/acquire/",
            {"credit_account": self.lekki_ledger.code},
        )
        self.assertRefusedAsUnknown(response)

    def test_a_bank_adjustment_countered_on_it(self):
        from vs_finance.models import BankStatementLine

        line = BankStatementLine.objects.create(
            bank_account=self.ikeja_bank, txn_date=JAN, amount=-500, description="Charges",
        )
        client = self.bursar("finance.bankaccount.reconcile")
        for field in ("counter_account", "counter_code"):
            with self.subTest(field=field):
                self.assertRefusedAsUnknown(self.post(
                    client, f"finance/statement-lines/{line.pk}/adjust/",
                    {field: self.lekki_ledger.code}))

    def test_a_payout_sourced_from_it(self):
        from vs_procurement.models import Vendor

        # Ikeja's own vendor: a payout reaches its vendor exclusively.
        vendor = Vendor.objects.create(
            entity=self.books, code="PAYEE", name="Payee Ltd", branch=self.ikeja,
            payable_account=Account.objects.get(entity=self.books, code="2100"),
        )
        response = self.post(
            self.bursar("payments.payout.create"), "payments/payouts/",
            {"amount": 5_000, "vendor": vendor.pk, "source_account": self.lekki_ledger.code},
            HTTP_IDEMPOTENCY_KEY="ledger-reach-1",
        )
        self.assertRefusedAsUnknown(response)

    def test_a_vendor_account_in_procurement(self):
        response = self.post(self.bursar("procurement.vendor.create"), "procurement/vendors/", {
            "code": "LEDG", "name": "Ledger Vendor", "payable_account": self.lekki_ledger.code,
        })
        self.assertRefusedAsUnknown(response)

    def test_a_school_default_pointed_at_it(self):
        from vs_finance.account_mappings import resolve_mapped_account
        from vs_finance.constants import AccountMappingKey

        client = self.bursar("finance.settings.update")
        response = client.patch(
            f"/v1/finance/settings/account-mappings/?entity={self.books.code}",
            # The mappings screen sends ids, which is how this route reads a number.
            {"mappings": {"CASH_BANK": self.lekki_ledger.pk}}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "SHARED_RECORD_READ_ONLY")
        self.assertNotEqual(
            resolve_mapped_account(self.books, AccountMappingKey.CASH_BANK), self.lekki_ledger,
        )

    def test_editing_the_ledger_account_itself(self):
        client = self.bursar("finance.account.update", "finance.account.view")
        refused = client.patch(
            f"/v1/finance/accounts/{self.lekki_ledger.pk}/?entity={self.books.code}",
            {"is_active": False}, format="json",
        )
        self.assertEqual(refused.status_code, 404, refused.data)
        self.lekki_ledger.refresh_from_db()
        self.assertTrue(self.lekki_ledger.is_active)

        own = client.patch(
            f"/v1/finance/accounts/{self.ikeja_bank.gl_account.pk}/?entity={self.books.code}",
            {"name": "Ikeja Collections Ledger"}, format="json",
        )
        self.assertEqual(own.status_code, 200, own.data)


class LedgerOfAnotherBranchOnABranchDocumentTests(_LedgerReachFixture):
    """A branch's document names its own branch's bank ledger and no other.

    Mrs Okafor covers Ikeja and Lekki, so Lekki's bank ledger is theirs to name.
    On an Ikeja document it is refused all the same, with the 400 that choosing
    Lekki's bank account gets, because the code moves the same money. A ledger
    account behind no bank account is unaffected.
    """

    def okafor(self, *keys):
        n = next(_people)
        user = self.user_for(self.tenant, f"okafor-ledger-{n}@corona.test")
        for branch in (self.ikeja, self.lekki):
            self.grant(user, *keys, tenant=self.tenant,
                       role_key=f"okafor-ledger-{n}-{branch.pk}", branch=branch)
        return TenantAPIClient(user=user)

    def assertOwnBranchOnly(self, client, path, body, field, *, noun, verb,
                            written=None, **extra):
        message = (f"This {noun} belongs to Ikeja Branch. "
                   f"{verb} an Ikeja Branch account.")
        refused = self.post(client, path, {**body, field: self.lekki_ledger.code}, **extra)
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn(message, str(refused.data))
        if written is not None:
            self.assertEqual(written(), 0, f"{path} wrote a row before refusing")
        unbranched = self.post(
            client, path, {**body, field: self.shared_bank.gl_account.code}, **extra)
        self.assertIn("No account", str(unbranched.data))
        accepted = self.post(
            client, path, {**body, field: self.ikeja_bank.gl_account.code}, **extra)
        self.assertNotIn("belongs to Ikeja Branch", str(accepted.data))
        self.assertNotIn("No account", str(accepted.data))
        return accepted

    def test_an_asset_bought_on_another_branchs_bank_ledger(self):
        asset = self.fixed_asset(self.books, "Ikeja Bus", self.ikeja)
        self.assertOwnBranchOnly(
            self.okafor("finance.fixedasset.acquire"),
            f"finance/fixed-assets/{asset.pk}/acquire/", {}, "credit_account",
            noun="asset", verb="Pay it from",
        )

    def test_a_ledger_behind_no_bank_account_is_unaffected(self):
        asset = self.fixed_asset(self.books, "Ikeja Van", self.ikeja)
        response = self.post(
            self.okafor("finance.fixedasset.acquire"),
            f"finance/fixed-assets/{asset.pk}/acquire/", {"credit_account": "2100"},
        )
        self.assertNotIn("belongs to Ikeja Branch", str(response.data))
        self.assertNotIn("No account", str(response.data))

    def test_a_customer_receipt_deposited_into_it(self):
        customer = self.customer(self.books, "CRCPO", self.ikeja)
        self.assertOwnBranchOnly(
            self.okafor("finance.payment.create"),
            f"finance/customers/{customer.code}/receipt/",
            {"amount": 5_000, "payment_date": JAN.isoformat()}, "deposit_account",
            noun="receipt", verb="Deposit it into",
            written=Payment.objects.filter(customer=customer, deposit_account=self.lekki_ledger).count,
        )

    def test_a_shared_customers_receipt_lands_in_its_own_branchs_bank(self):
        """The receipt takes the branch they name, and deposits only there."""
        customer = self.customer(self.books, "CRALL", None)
        body = {"amount": 5_000, "payment_date": JAN.isoformat(),
                "deposit_account": self.lekki_ledger.code}
        client = self.okafor("finance.payment.create")

        crossed = self.post(client, f"finance/customers/{customer.code}/receipt/",
                            {**body, "branch": self.ikeja.pk})
        self.assertEqual(crossed.status_code, 400, crossed.data)
        self.assertIn("This receipt belongs to Ikeja Branch. Deposit it into an Ikeja "
                      "Branch account.", str(crossed.data))

        own = self.post(client, f"finance/customers/{customer.code}/receipt/",
                        {**body, "branch": self.lekki.pk})
        self.assertNotIn("belongs to", str(own.data))
        self.assertNotIn("No account", str(own.data))

    def test_an_invoice_payment_deposited_into_it(self):
        from vs_finance.models import Payment

        invoice = self.invoice(self.books, self.customer(self.books, "CINVP", self.ikeja), self.ikeja)
        invoice.status = "POSTED"
        invoice.save(update_fields=["status"])
        client = self.okafor("finance.payment.create")
        refused = self.post(client, f"finance/invoices/{invoice.pk}/pay/", {
            "amount": 5_000, "payment_date": JAN.isoformat(),
            "deposit_account": self.lekki_ledger.code,
        })
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("This receipt belongs to Ikeja Branch. Deposit it into an Ikeja "
                      "Branch account.", str(refused.data))
        self.assertFalse(Payment.objects.filter(customer=invoice.customer).exists())

    def test_a_bank_adjustment_countered_on_it(self):
        from vs_finance.models import BankStatementLine

        line = BankStatementLine.objects.create(
            bank_account=self.ikeja_bank, txn_date=JAN, amount=-500, description="Charges",
        )
        self.assertOwnBranchOnly(
            self.okafor("finance.bankaccount.reconcile"),
            f"finance/statement-lines/{line.pk}/adjust/", {}, "counter_code",
            noun="bank adjustment", verb="Book it against",
        )

    def test_a_payment_request_and_a_virtual_account_deposited_into_it(self):
        """A payment request opens a checkout with the provider, faked here."""
        from vs_payments.providers import registry
        from vs_payments.providers.fake import FakeProvider

        registry.register("PAYSTACK", FakeProvider(secret="test-secret"))
        self.addCleanup(registry.unregister, "PAYSTACK")
        customer = self.customer(self.books, "CPAYR", self.ikeja)
        client = self.okafor("payments.collection.create", "payments.virtual_account.create")
        accepted = self.assertOwnBranchOnly(
            client, "payments/collections/", {"amount": 5_000, "customer": customer.code},
            "deposit_account", noun="payment request", verb="Deposit it into",
        )
        self.assertIn(accepted.status_code, (200, 201), accepted.data)
        refused = self.post(client, "payments/virtual-accounts/", {
            "customer": customer.code, "deposit_account": self.lekki_ledger.code,
        })
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn("This virtual account belongs to Ikeja Branch.", str(refused.data))
