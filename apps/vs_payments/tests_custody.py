"""Online money is booked to gateway clearing, settles per branch, and follows the tenant's custody mode.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Mrs Adeyemi pays
Lekki N180,000 online: the books say the provider has it (gateway clearing), not
Lekki's Zenith account, until Zenith's statement shows N178,000 arriving and a
bursar matches it, which books the N2,000 fee as Lekki's bank charge. When Corona
takes payments directly, each checkout names the branch's own provider
subaccount, a branch without one cannot take online payments, and online
payouts are refused. Every branch prints its own collection account.
"""
from __future__ import annotations

import datetime
import itertools
from unittest.mock import patch

from django.apps import apps as django_apps
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.banking import unmatch_line
from vs_finance.constants import BankLineStatus
from vs_finance.models import (
    Account,
    BankAccount,
    BankStatementLine,
    FinanceAccountMapping,
    FiscalPeriod,
    FiscalYear,
    JournalLine,
    LedgerEntity,
)
from vs_finance.receivables import post_invoice
from vs_finance.seed import seed_chart_of_accounts
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_procurement.models import Vendor

from . import custody, reconciliation, services, settlement
from .constants import CollectionStatus, CustodyMode
from .custody import OnlinePayoutsNotOfferedError, SubaccountMissingError
from .exceptions import PaymentStateError
from .models import CollectionIntent, PaymentCustodySettings, PayoutBatch
from .providers import registry
from .providers.fake import FakeProvider

_people = itertools.count(1)


class _CustodyFixture(_FinanceBranchFixture):
    """Corona (three branches) and Single Site (one), each with a year of open periods.

    Corona's Ikeja and Lekki each keep a collection account; Yaba keeps none.
    Single Site's collection account has not been given a branch, which at a
    tenant with one branch is the branch's own.
    """

    @classmethod
    def build_books(cls, code, tenant):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        year = tenant_today(tenant).year
        fiscal_year = FiscalYear.objects.create(
            entity=entity, year=year, start_date=datetime.date(year, 1, 1),
            end_date=datetime.date(year, 12, 31),
        )
        for month in range(1, 13):
            start = datetime.date(year, month, 1)
            FiscalPeriod.objects.create(
                entity=entity, fiscal_year=fiscal_year, period_no=month,
                name=f"{year}-{month:02d}", start_date=start,
                end_date=custody.next_month_start(start) - datetime.timedelta(days=1),
            )
        return entity

    @classmethod
    def bank(cls, entity, code, name, branch, *, collection=True):
        gl = Account.objects.create(
            entity=entity, code=code, name=name, account_type="ASSET", is_postable=True)
        return BankAccount.objects.create(
            entity=entity, name=name, branch=branch, gl_account=gl, bank_name=name,
            account_number=f"00{code}0000", is_primary_collection=collection,
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_bank = cls.bank(cls.books, "1131", "Ikeja GTBank", cls.ikeja)
        cls.lekki_bank = cls.bank(cls.books, "1132", "Lekki Zenith", cls.lekki)
        cls.solo_bank = cls.bank(cls.solo_books, "1131", "Main Access", None)
        cls.adeyemi = cls.customer(cls.books, "CADEY", cls.lekki)
        cls.okafor = cls.customer(cls.books, "COKAF", cls.ikeja)
        cls.solo_parent = cls.customer(cls.solo_books, "CSOLO", None)

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        registry.register("PAYSTACK", self.fake)
        self.addCleanup(registry.unregister, "PAYSTACK")

    # -- helpers --------------------------------------------------------------- #

    def paid(self, entity, customer, amount, *, fee=2_000, invoice=None):
        """A collection the provider has confirmed, reporting ``fee``."""
        intent = services.initiate_collection(
            entity=entity, amount=amount, customer=customer, invoice=invoice)
        self.fake.forced_status[intent.reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[intent.reference] = amount
        self.fake.forced_fee[intent.reference] = fee
        return services.confirm_collection(intent)

    def line(self, bank, amount, *, day=None, reference="PSTK-SETTLE"):
        return BankStatementLine.objects.create(
            bank_account=bank, txn_date=day or tenant_today(bank.entity.tenant),
            amount=amount, description="Paystack settlement", reference=reference,
        )

    def balance(self, account, branch=None):
        lines = JournalLine.objects.filter(account=account, entry__status__in=("POSTED", "REVERSED"))
        if branch is not None:
            lines = lines.filter(entry__branch=branch)
        return sum(line.debit - line.credit for line in lines)

    def clearing(self, entity):
        return Account.objects.get(entity=entity, code="1125")

    def direct(self, tenant):
        PaymentCustodySettings.objects.update_or_create(
            tenant=tenant, defaults={"mode": CustodyMode.DIRECT})

    @classmethod
    def settle_directly(cls):
        """Corona and Single Site take payments directly, Ikeja, Lekki and Main set up.

        A payment the provider settles to the branch's own bank is the one a bank
        statement line settles; a held payment reaches the bank only through the
        platform's settlement run (``tests_custody_held``).
        """
        for bank, code in ((cls.ikeja_bank, "ACCT_IKJ"), (cls.lekki_bank, "ACCT_LEK"),
                           (cls.solo_bank, "ACCT_SOLO")):
            BankAccount.objects.filter(pk=bank.pk).update(
                gateway_subaccount_code=code, gateway_subaccount_provider="PAYSTACK")
        for tenant in (cls.tenant, cls.solo_tenant):
            PaymentCustodySettings.objects.update_or_create(
                tenant=tenant, defaults={"mode": CustodyMode.DIRECT})


class GatewayClearingTests(_CustodyFixture):
    """A confirmed payment waits in clearing; its settlement moves it to the branch's bank."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.settle_directly()

    def test_a_confirmed_payment_debits_clearing_not_the_bank(self):
        for entity, customer, bank, branch in (
                (self.books, self.adeyemi, self.lekki_bank, self.lekki),
                (self.solo_books, self.solo_parent, self.solo_bank, self.solo_main)):
            with self.subTest(entity=entity.code):
                intent = self.paid(entity, customer, 180_000)
                self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
                self.assertEqual(intent.branch_id, branch.pk)
                self.assertEqual(intent.fee, 2_000)
                self.assertEqual(intent.clearing_account, self.clearing(entity))
                self.assertTrue(intent.awaits_settlement)
                self.assertEqual(intent.payment.deposit_account, self.clearing(entity))
                self.assertEqual(self.balance(self.clearing(entity), branch), 180_000)
                self.assertEqual(self.balance(bank.gl_account), 0)

    def test_a_settlement_moves_clearing_to_the_bank_and_books_the_fee(self):
        first = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        second = self.paid(self.books, self.adeyemi, 50_000, fee=750)
        line = self.line(self.lekki_bank, 227_250)

        entry = settlement.settle_collections(line, [first.pk, second.pk])

        self.assertEqual(entry.branch_id, self.lekki.pk)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 227_250)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        charges = Account.objects.get(entity=self.books, code="5500")
        self.assertEqual(self.balance(charges, self.lekki), 2_750)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.MATCHED)
        self.assertEqual(line.adjusting_journal, entry)
        from vs_finance.posting import _journal_document_owner

        self.assertEqual(_journal_document_owner(entry), line)  # Only unmatching it reverses it.
        for intent in (first, second):
            intent.refresh_from_db()
            self.assertFalse(intent.awaits_settlement)

    def test_a_settlement_at_a_one_branch_school_is_the_branchs(self):
        intent = self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        entry = settlement.settle_collections(self.line(self.solo_bank, 39_400), [intent.pk])
        self.assertEqual(entry.branch_id, self.solo_main.pk)
        self.assertEqual(self.balance(self.clearing(self.solo_books)), 0)

    def test_a_branchs_payments_settle_only_into_its_own_bank(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        line = self.line(self.ikeja_bank, 178_000)
        with self.assertRaises(PaymentStateError) as caught:
            settlement.settle_collections(line, [intent.pk])
        self.assertIn("belongs to another branch than Ikeja GTBank", str(caught.exception))
        line.refresh_from_db()
        self.assertEqual(line.status, BankLineStatus.UNMATCHED)

    def test_a_line_carrying_other_money_is_not_booked_as_a_fee(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        with self.assertRaises(PaymentStateError) as caught:
            settlement.settle_collections(self.line(self.lekki_bank, 170_000), [intent.pk])
        self.assertIn("fees on these payments come to 2000 kobo", str(caught.exception))
        with self.assertRaises(PaymentStateError):
            settlement.settle_collections(self.line(self.lekki_bank, 190_000), [intent.pk])

    def test_a_line_dated_before_the_payment_cannot_settle_it(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        yesterday = tenant_today(self.tenant) - datetime.timedelta(days=1)
        with self.assertRaises(PaymentStateError) as caught:
            settlement.settle_collections(self.line(self.lekki_bank, 178_000, day=yesterday), [intent.pk])
        self.assertIn("before a payment it settles was received", str(caught.exception))

    def test_a_payment_settles_once_and_again_after_unmatching(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        line = self.line(self.lekki_bank, 178_000)
        settlement.settle_collections(line, [intent.pk])
        with self.assertRaises(PaymentStateError):
            settlement.settle_collections(self.line(self.lekki_bank, 178_000), [intent.pk])

        line.refresh_from_db()
        unmatch_line(line)
        intent.refresh_from_db()
        self.assertTrue(intent.awaits_settlement)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 180_000)
        settlement.settle_collections(line, [intent.pk])
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)

    def test_clearing_is_a_control_account_a_hand_journal_cannot_touch(self):
        from vs_finance.control_accounts import control_accounts

        self.assertIn(self.clearing(self.books).pk, control_accounts(self.books))

    def test_the_report_suggests_the_line_then_shows_the_payment_settled(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        line = self.line(self.lekki_bank, 178_000)
        before = reconciliation.settlement_reconciliation(self.books)
        self.assertEqual(
            [(s["bank_line_id"], s["collection_ids"], s["fee"]) for s in before.suggested_settlements],
            [(line.pk, [intent.pk], 2_000)],
        )
        row = next(r for r in before.rows if r.gateway_id == intent.pk)
        self.assertFalse(row.settled)

        settlement.settle_collections(line, [intent.pk])
        after = reconciliation.settlement_reconciliation(self.books)
        row = next(r for r in after.rows if r.gateway_id == intent.pk)
        self.assertEqual((row.settled, row.match_basis, row.matched_bank_line_id, row.fee_amount),
                         (True, "settlement", line.pk, 2_000))
        self.assertEqual(after.unmatched_bank_lines, [])
        self.assertEqual(after.suggested_settlements, [])

    def test_the_close_warns_about_payments_left_in_clearing(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        CollectionIntent.objects.filter(pk=intent.pk).update(
            confirmed_at=timezone.now() - datetime.timedelta(days=30))
        period = FiscalPeriod.objects.get(
            entity=self.books, start_date__lte=tenant_today(self.tenant),
            end_date__gte=tenant_today(self.tenant))
        warning = settlement.gateway_clearing_current(self.books, period)
        self.assertEqual((warning.passed, warning.blocking), (False, False))
        self.assertIn("1 online payment(s), 180000 kobo", warning.detail)

        settlement.settle_collections(self.line(self.lekki_bank, 178_000), [intent.pk])
        self.assertTrue(settlement.gateway_clearing_current(self.books, period).passed)
        self.assertIsNone(settlement.gateway_clearing_current(self.solo_books, period))

    def test_a_branch_close_warns_only_about_that_branch_clearing(self):
        lekki = self.paid(self.books, self.adeyemi, 180_000)
        ikeja = self.paid(self.books, self.okafor, 75_000)
        CollectionIntent.objects.filter(pk__in=(lekki.pk, ikeja.pk)).update(
            confirmed_at=timezone.now() - datetime.timedelta(days=30),
        )
        period = FiscalPeriod.objects.get(
            entity=self.books, start_date__lte=tenant_today(self.tenant),
            end_date__gte=tenant_today(self.tenant),
        )

        warning = settlement.gateway_clearing_current(
            self.books, period, branch=self.lekki,
        )

        self.assertIn("1 online payment(s), 180000 kobo", warning.detail)


class CustodyModeTests(_CustodyFixture):
    """The mode changes only from a month start, and direct needs every branch set up."""

    def subaccount(self, bank, code):
        bank.gateway_subaccount_code, bank.gateway_subaccount_provider = code, "PAYSTACK"
        bank.save(update_fields=["gateway_subaccount_code", "gateway_subaccount_provider"])

    def test_a_tenant_is_held_until_it_chooses(self):
        self.assertEqual(custody.custody_mode(self.tenant), CustodyMode.HELD)
        self.assertFalse(custody.is_direct(self.books))

    def test_direct_waits_for_every_branch_and_for_the_month(self):
        with self.assertRaises(ValidationError) as caught:
            custody.update_custody_settings(entity=self.books, data={"mode": "DIRECT"})
        self.assertIn("Not yet: Ikeja Branch, Lekki Branch, Yaba Branch", str(caught.exception))

        self.subaccount(self.ikeja_bank, "ACCT_IKJ")
        self.subaccount(self.lekki_bank, "ACCT_LEK")
        self.subaccount(self.bank(self.books, "1133", "Yaba UBA", self.yaba), "ACCT_YAB")
        row = custody.update_custody_settings(entity=self.books, data={"mode": "direct"})
        today = tenant_today(self.tenant)
        self.assertEqual((row.pending_mode, row.pending_from),
                         (CustodyMode.DIRECT, custody.next_month_start(today)))
        self.assertEqual(custody.custody_mode(self.tenant), CustodyMode.HELD)
        # Still held on the day itself, until the daily task finds nothing held.
        self.assertEqual(custody.custody_mode(self.tenant, on=row.pending_from), CustodyMode.HELD)

        row = custody.update_custody_settings(entity=self.books, data={"mode": "HELD"})
        self.assertEqual((row.pending_mode, row.pending_from), ("", None))

    def test_the_settlement_interval_is_one_to_seven_days(self):
        for bad in (0, 8, "two", True):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                custody.update_custody_settings(
                    entity=self.books, data={"settlement_interval_days": bad})
        row = custody.update_custody_settings(
            entity=self.solo_books, data={"settlement_interval_days": 3})
        self.assertEqual((row.settlement_interval_days, row.mode), (3, CustodyMode.HELD))

    def test_a_direct_checkout_names_the_branchs_subaccount(self):
        self.direct(self.tenant)
        self.subaccount(self.lekki_bank, "ACCT_LEK")
        intent = services.initiate_collection(entity=self.books, amount=180_000, customer=self.adeyemi)
        self.assertEqual(self.fake.subaccounts_named[intent.reference], "ACCT_LEK")
        self.assertEqual(intent.deposit_account, self.lekki_bank.gl_account)

        with self.assertRaises(SubaccountMissingError) as caught:
            services.initiate_collection(entity=self.books, amount=50_000, customer=self.okafor)
        self.assertEqual(caught.exception.http_status, 409)
        self.assertIn("Ikeja Branch's collection account Ikeja GTBank is not set up",
                      str(caught.exception))
        self.assertFalse(CollectionIntent.objects.filter(customer=self.okafor).exists())

    def test_a_held_checkout_names_no_subaccount(self):
        self.subaccount(self.lekki_bank, "ACCT_LEK")
        intent = services.initiate_collection(entity=self.books, amount=180_000, customer=self.adeyemi)
        self.assertNotIn(intent.reference, self.fake.subaccounts_named)

    def test_a_direct_virtual_account_names_the_branchs_subaccount(self):
        self.direct(self.solo_tenant)
        self.subaccount(self.solo_bank, "ACCT_SOLO")
        account = services.create_virtual_account(entity=self.solo_books, customer=self.solo_parent)
        self.assertEqual(account.branch_id, self.solo_main.pk)
        self.assertIn("ACCT_SOLO", self.fake.subaccounts_named.values())

    def test_a_direct_tenant_has_no_online_payouts(self):
        self.direct(self.tenant)
        with self.assertRaises(OnlinePayoutsNotOfferedError) as caught:
            services.create_payout_batch(entity=self.books, items=[{"amount": 1_000}])
        self.assertEqual(caught.exception.http_status, 409)
        self.assertIn("Pay the supplier from the bank and record the payment", str(caught.exception))


class SubaccountTests(_CustodyFixture):
    def test_a_branchs_collection_account_gets_a_subaccount_then_refreshes_it(self):
        bank = custody.save_collection_subaccount(
            entity=self.books, bank_account=self.lekki_bank, settlement_bank_code="057")
        code = bank.gateway_subaccount_code
        self.assertEqual((bank.gateway_subaccount_provider, self.fake.subaccounts[code]["settlement_bank"]),
                         ("PAYSTACK", "057"))
        self.assertEqual(self.fake.subaccounts[code]["percentage_charge"], 0)
        self.assertEqual(self.fake.subaccounts[code]["business_name"], "FINMULTI Books - Lekki Branch")

        again = custody.save_collection_subaccount(
            entity=self.books, bank_account=bank, settlement_bank_code="058")
        self.assertEqual(again.gateway_subaccount_code, code)
        self.assertEqual(self.fake.subaccounts[code]["settlement_bank"], "058")

    def test_only_a_collection_account_takes_one(self):
        spare = self.bank(self.books, "1134", "Lekki Spare", self.lekki, collection=False)
        with self.assertRaises(ValidationError):
            custody.save_collection_subaccount(
                entity=self.books, bank_account=spare, settlement_bank_code="057")
        with self.assertRaises(ValidationError):
            custody.save_collection_subaccount(
                entity=self.solo_books, bank_account=self.lekki_bank, settlement_bank_code="057")


class GatewayRecordBranchTests(_CustodyFixture):
    """Each gateway record names its branch when it is made."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vendor = Vendor.objects.create(
            entity=cls.books, code="SUPPX", name="Supplier Ltd",
            payable_account=Account.objects.get(entity=cls.books, code="2100"),
            default_expense_account=Account.objects.get(entity=cls.books, code="5300"),
            bank_name="GTBank", bank_code="058", bank_account_name="Supplier Ltd",
            bank_account_number="0123456789", kyc_status="VERIFIED",
        )

    def test_a_collection_for_an_invoice_is_the_invoices_branch(self):
        invoice = self.invoice(self.books, self.okafor, self.lekki)
        invoice.lines.update(revenue_account=Account.objects.get(entity=self.books, code="4100"))
        post_invoice(invoice)
        intent = services.initiate_collection(
            entity=self.books, amount=10_000, customer=self.okafor, invoice=invoice)
        self.assertEqual(intent.branch_id, self.lekki.pk)

    def test_a_batch_pays_from_one_branch(self):
        line = {"amount": 10_000, "vendor": self.vendor}
        batch = services.create_payout_batch(
            entity=self.books, items=[dict(line), dict(line)],
            source_account=self.ikeja_bank.gl_account)
        self.assertEqual(batch.branch_id, self.ikeja.pk)
        self.assertEqual(set(batch.instructions.values_list("branch_id", flat=True)), {self.ikeja.pk})

        with self.assertRaises(ValidationError) as caught:
            services.create_payout_batch(entity=self.books, items=[
                {**line, "source_account": self.ikeja_bank.gl_account},
                {**line, "source_account": self.lekki_bank.gl_account},
            ])
        self.assertIn("different branches", str(caught.exception))
        self.assertEqual(PayoutBatch.objects.filter(entity=self.books).count(), 1)


class CollectionAccountPerBranchTests(_CustodyFixture):
    """Each branch prints its own collection account."""

    def test_documents_print_the_branchs_own_account(self):
        from vs_finance.documents import primary_collection_account

        self.assertEqual(primary_collection_account(self.books, self.lekki), self.lekki_bank)
        self.assertEqual(primary_collection_account(self.books, self.ikeja), self.ikeja_bank)
        self.assertIsNone(primary_collection_account(self.books, self.yaba))
        self.assertEqual(primary_collection_account(self.solo_books, self.solo_main), self.solo_bank)

    def test_choosing_a_branchs_account_leaves_the_others(self):
        from vs_finance.document_settings import update_finance_document_settings

        second = self.bank(self.books, "1135", "Lekki Access", self.lekki, collection=False)
        update_finance_document_settings(
            entity=self.books, data={"primary_collection_bank_account": second.pk}, actor_user=None)
        flagged = set(BankAccount.objects.filter(
            entity=self.books, is_primary_collection=True).values_list("pk", flat=True))
        self.assertEqual(flagged, {self.ikeja_bank.pk, second.pk})


class GatewayClearingMigrationTests(_CustodyFixture):
    """Existing books get a clearing account, at another code when 1125 is taken."""

    def test_a_taken_code_moves_the_account_and_maps_the_role(self):
        from importlib import import_module

        migration = import_module("vs_finance.migrations.0046_gateway_clearing_accounts")
        Account.objects.filter(entity=self.solo_books, code="1125").delete()
        Account.objects.create(entity=self.solo_books, code="1125", name="Old Ops",
                               account_type="ASSET", is_postable=True)
        Account.objects.filter(entity=self.books, code="1125").delete()

        migration.forwards(django_apps, None)

        moved = FinanceAccountMapping.objects.get(entity=self.solo_books, key="GATEWAY_CLEARING")
        self.assertEqual((moved.account.code, moved.account.name), ("1126", "Gateway Clearing"))
        self.assertEqual(Account.objects.get(entity=self.books, code="1125").name, "Gateway Clearing")
        self.assertFalse(FinanceAccountMapping.objects.filter(
            entity=self.books, key="GATEWAY_CLEARING").exists())


class CustodyEndpointTests(_CustodyFixture):
    """Custody and subaccounts are whole-tenant; settlements stay within the caller's branches."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.settle_directly()

    def client_for(self, *keys, branch=None, tenant=None):
        tenant = tenant or self.tenant
        n = next(_people)
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"custody-{n}@corona.test"), *keys,
            tenant=tenant, role_key=f"custody-{n}", branch=branch,
        ))

    def url(self, path, entity=None):
        return f"/v1/payments/{path}?entity={(entity or self.books).code}"

    def test_only_a_whole_tenant_administrator_changes_custody(self):
        body = {"settlement_interval_days": 2}
        bound = self.client_for("payments.settings.view", "payments.settings.update", branch=self.lekki)
        self.assertEqual(bound.get(self.url("settings/custody/")).status_code, 200)
        self.assertEqual(bound.patch(self.url("settings/custody/"), body, format="json").status_code, 403)
        reader = self.client_for("payments.settings.view")
        self.assertEqual(reader.patch(self.url("settings/custody/"), body, format="json").status_code, 403)

        admin = self.client_for("payments.settings.view", "payments.settings.update")
        response = admin.patch(self.url("settings/custody/"), body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["settings"]["settlement_interval_days"], 2)
        rows = {row["branch_name"]: row["collection_account"] for row in response.data["data"]["branches"]}
        self.assertEqual(rows["Lekki Branch"]["name"], "Lekki Zenith")
        self.assertIsNone(rows["Yaba Branch"])

    def test_a_branch_administrator_cannot_set_up_a_subaccount(self):
        body = {"bank_account": self.lekki_bank.pk, "settlement_bank_code": "057"}
        bound = self.client_for("payments.settings.update", branch=self.lekki)
        self.assertEqual(bound.post(self.url("subaccounts/"), body, format="json").status_code, 403)
        admin = self.client_for("payments.settings.update")
        response = admin.post(self.url("subaccounts/"), body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["subaccount_ready"])
        self.assertNotIn("account_number", response.data["data"])

    def test_a_settlement_is_booked_only_within_the_callers_branches(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        line = self.line(self.lekki_bank, 178_000)
        body = {"statement_line": line.pk, "collections": [intent.pk]}

        ikeja = self.client_for("payments.settlement.create", branch=self.ikeja)
        self.assertEqual(ikeja.post(self.url("settlements/"), body, format="json").status_code, 404)
        rival = self.client_for("payments.settlement.create", tenant=self.rival_tenant)
        self.assertEqual(rival.post(self.url("settlements/"), body, format="json").status_code, 404)
        unkeyed = self.client_for("payments.report.view", branch=self.lekki)
        self.assertEqual(unkeyed.post(self.url("settlements/"), body, format="json").status_code, 403)

        lekki = self.client_for("payments.settlement.create", branch=self.lekki)
        response = lekki.post(self.url("settlements/"), body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["data"]["gross"], response.data["data"]["fee"],
                          response.data["data"]["net"]), (180_000, 2_000, 178_000))


class CustodyModeForPayoutReadersTests(_CustodyFixture):
    """A payout reader learns the custody mode, and nothing else about the setting.

    The payout screens are offered only where the platform holds the tenant's
    online money, so the person who reads payouts has to be able to ask which
    mode is in force. Corona (three branches) is HELD with a move to direct
    pending; Single Site (one branch) is DIRECT.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        PaymentCustodySettings.objects.create(
            tenant=cls.tenant, mode=CustodyMode.HELD, pending_mode=CustodyMode.DIRECT,
            pending_from=custody.next_month_start(tenant_today(cls.tenant)),
            pending_note="Waiting: Corona Ikeja Branch has N12,400.00 held.",
        )
        PaymentCustodySettings.objects.create(tenant=cls.solo_tenant, mode=CustodyMode.DIRECT)

    def client_for(self, *keys, branch=None, tenant=None):
        tenant = tenant or self.tenant
        n = next(_people)
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"payout-reader-{n}@corona.test"), *keys,
            tenant=tenant, role_key=f"payout-reader-{n}", branch=branch,
        ))

    def read(self, client, books=None):
        return client.get(f"/v1/payments/settings/custody/?entity={(books or self.books).code}")

    def test_a_payout_reader_at_a_held_school_gets_the_mode_only(self):
        for branch in (None, self.lekki):
            response = self.read(self.client_for("payments.payout.view", branch=branch))
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["data"], {"settings": {"mode": "HELD"}})
            for hidden in ("Ikeja", "Lekki", "Zenith", "12,400", "DIRECT"):
                self.assertNotIn(hidden, str(response.data))

    def test_a_payout_reader_at_a_direct_school_gets_the_mode_only(self):
        for branch in (None, self.solo_main):
            response = self.read(
                self.client_for("payments.payout.view", branch=branch, tenant=self.solo_tenant),
                self.solo_books)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["data"], {"settings": {"mode": "DIRECT"}})

    def test_a_caller_with_neither_key_is_refused(self):
        for keys in ((), ("payments.report.view", "payments.payout_batch.submit")):
            response = self.read(self.client_for(*keys))
            self.assertEqual(response.status_code, 403, response.data)

    def test_a_settings_reader_keeps_the_full_answer(self):
        full = self.read(self.client_for("payments.settings.view"))
        self.assertEqual(full.status_code, 200, full.data)
        settings = full.data["data"]["settings"]
        self.assertEqual(set(full.data["data"]), {"settings", "branches"})
        self.assertEqual((settings["mode"], settings["pending_mode"]), ("HELD", "DIRECT"))
        self.assertIn("settlement_interval_days", settings)
        self.assertEqual(len(full.data["data"]["branches"]), 3)

        both = self.read(self.client_for("payments.settings.view", "payments.payout.view"))
        self.assertEqual(both.data["data"], full.data["data"])

        bound = self.read(self.client_for("payments.settings.view", branch=self.lekki))
        self.assertEqual([row["branch_name"] for row in bound.data["data"]["branches"]],
                         ["Lekki Branch"])

    def test_a_payout_reader_cannot_change_the_setting(self):
        body = {"mode": "HELD", "settlement_interval_days": 2}
        for branch in (None, self.lekki):
            client = self.client_for("payments.payout.view", "payments.payout.create", branch=branch)
            response = client.patch(
                f"/v1/payments/settings/custody/?entity={self.books.code}", body, format="json")
            self.assertEqual(response.status_code, 403, response.data)
        row = PaymentCustodySettings.objects.get(tenant=self.tenant)
        self.assertEqual((row.pending_mode, row.settlement_interval_days),
                         (CustodyMode.DIRECT, PaymentCustodySettings().settlement_interval_days))

    def test_another_tenants_mode_is_never_answered(self):
        corona = self.client_for("payments.payout.view")
        self.assertEqual(self.read(corona, self.solo_books).status_code, 404)
        self.assertEqual(self.read(corona, self.rival_books).status_code, 404)
        rival = self.client_for("payments.payout.view", tenant=self.rival_tenant)
        self.assertEqual(self.read(rival).status_code, 404)
        own = self.read(rival, self.rival_books)
        self.assertEqual(own.data["data"], {"settings": {"mode": "HELD"}})


class PaystackSubaccountWireTests(SimpleTestCase):
    """The Paystack fields direct custody relies on, as the adapter sends and reads them.

    Each is a fact to confirm against Paystack's documentation (see the adapter's
    module docstring); this pins what the adapter does with them.
    """

    PATCH_TARGET = "vs_payments.providers.paystack.request_json"

    def setUp(self):
        from .providers.paystack import PaystackProvider

        self.provider = PaystackProvider(secret_key="sk_test_x")

    def sent(self, call):
        method, url = call.args[:2]
        return method, url.split("api.paystack.co", 1)[1], call.kwargs.get("body")

    def test_a_checkout_names_the_subaccount_and_its_bearer(self):
        ok = {"status": True, "data": {"reference": "R1", "authorization_url": "https://pay/x"}}
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            self.provider.create_checkout(reference="R1", amount=100, currency="NGN",
                                          subaccount="ACCT_LEK")
        method, path, body = self.sent(call.call_args)
        self.assertEqual((method, path), ("POST", "/transaction/initialize"))
        self.assertEqual((body["subaccount"], body["bearer"]), ("ACCT_LEK", "subaccount"))

        with patch(self.PATCH_TARGET, return_value=ok) as call:
            self.provider.create_checkout(reference="R2", amount=100, currency="NGN")
        self.assertNotIn("subaccount", self.sent(call.call_args)[2])

    def test_verify_reads_the_fee(self):
        resp = {"status": True, "data": {"status": "success", "amount": 180000, "fees": 2000}}
        with patch(self.PATCH_TARGET, return_value=resp):
            self.assertEqual(self.provider.verify_collection(reference="R1").fee, 2000)
        resp["data"].pop("fees")
        with patch(self.PATCH_TARGET, return_value=resp):
            self.assertIsNone(self.provider.verify_collection(reference="R1").fee)

    def test_a_subaccount_is_created_then_pointed_again(self):
        created = {"status": True, "data": {"subaccount_code": "ACCT_x1", "account_name": "LEKKI"}}
        with patch(self.PATCH_TARGET, return_value=created) as call:
            result = self.provider.create_subaccount(
                business_name="Corona - Lekki", settlement_bank_code="057",
                account_number="0011223344")
        self.assertEqual((result.subaccount_code, result.account_name), ("ACCT_x1", "LEKKI"))
        self.assertEqual(self.sent(call.call_args), ("POST", "/subaccount", {
            "business_name": "Corona - Lekki", "bank_code": "057", "settlement_bank": "057",
            "account_number": "0011223344", "percentage_charge": 0,
        }))

        with patch(self.PATCH_TARGET, return_value={"status": True, "data": {}}) as call:
            result = self.provider.update_subaccount(
                subaccount_code="ACCT_x1", business_name="Corona - Lekki",
                settlement_bank_code="058", account_number="0011223344")
        method, path, body = self.sent(call.call_args)
        self.assertEqual(
            (method, path, body["bank_code"], body["settlement_bank"]),
            ("PUT", "/subaccount/ACCT_x1", "058", "058"),
        )
        self.assertEqual(result.subaccount_code, "ACCT_x1")
