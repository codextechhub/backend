"""A shared physical bank account becomes one branch-owned ledger per branch.

Bright Star's old GTBank record predates branch books. Its statements and posted
journals remain where they were, while an agreed cutover moves the exact signed
balance into one new bank ledger for Ikeja and one for Lekki. The old account is
then inactive and cannot be split a second time.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from core.test_utils import TenantAPIClient
from vs_rbac.scoping import BranchScope

from .branch_ledger import ledger_lines
from .constants import JournalSource, PeriodStatus
from .exceptions import BankAccountSplitError, PeriodClosedError
from .models import (
    Account,
    BankAccount,
    BankStatement,
    BankStatementLine,
    FinanceAuditLog,
    JournalEntry,
    JournalLine,
)
from .posting import post_journal, resolve_period
from .reports import balance_sheet
from .tests_branch_scope import _FinanceBranchFixture


JAN_10 = datetime.date(2026, 1, 10)
JAN_15 = datetime.date(2026, 1, 15)
JAN_20 = datetime.date(2026, 1, 20)


def net(account) -> int:
    """Posted debits less credits on one cash ledger."""
    return sum(
        int(line.debit or 0) - int(line.credit or 0)
        for line in ledger_lines().filter(account=account)
    )


class SharedBankAccountSplitTests(_FinanceBranchFixture):
    """Service and HTTP boundary for the one-time branch cutover."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.legacy_ledger = Account.objects.create(
            entity=cls.books,
            parent=Account.objects.get(entity=cls.books, code="1100"),
            code="1150",
            name="Shared GTBank ledger",
            account_type="ASSET",
            normal_balance="DEBIT",
            is_postable=True,
        )
        cls.legacy_bank = BankAccount.objects.create(
            entity=cls.books,
            branch=None,
            gl_account=cls.legacy_ledger,
            name="Shared GTBank",
            bank_name="GTBank",
            account_number="0123456789",
            is_primary=True,
            is_primary_collection=True,
            gateway_subaccount_code="legacy-provider-route",
            gateway_subaccount_provider="PAYSTACK",
            settlement_bank_code="058",
        )
        cls.historical_journal = cls.post_movement(
            cls.books, cls.legacy_ledger, 1_000_000, JAN_10, branch=cls.ikeja,
        )
        cls.statement = BankStatement.objects.create(
            bank_account=cls.legacy_bank,
            statement_date=JAN_10,
            opening_balance=0,
            closing_balance=1_000_000,
        )
        cls.whole_user = cls.grant(
            cls.user_for(cls.tenant, "whole-bank-split@fin.test"),
            "finance.bankaccount.update",
            tenant=cls.tenant,
            role_key="whole_bank_split",
        )
        cls.partial_user = cls.grant(
            cls.user_for(cls.tenant, "partial-bank-split@fin.test"),
            "finance.bankaccount.update",
            tenant=cls.tenant,
            role_key="partial_bank_split",
            branch=cls.ikeja,
        )

    @classmethod
    def post_movement(cls, entity, account, amount, on_date, *, branch=None):
        """Post cash against equity to establish a historical signed balance."""
        equity = Account.objects.get(entity=entity, code="3100")
        entry = JournalEntry.objects.create(
            entity=entity,
            branch=branch,
            date=on_date,
            period=resolve_period(entity, on_date),
            source=JournalSource.SYSTEM,
            narration="Historical shared-bank movement",
        )
        if amount >= 0:
            rows = ((account, amount, 0), (equity, 0, amount))
        else:
            rows = ((equity, -amount, 0), (account, 0, -amount))
        for line_no, (ledger, debit, credit) in enumerate(rows, start=1):
            JournalLine.objects.create(
                entry=entry,
                account=ledger,
                debit=debit,
                credit=credit,
                line_no=line_no,
            )
        post_journal(entry)
        return entry

    def allocations(self, *, ikeja=600_000, lekki=400_000):
        return [
            {
                "branch": self.ikeja,
                "opening_balance": ikeja,
                "bank_account_name": "Ikeja GTBank",
                "ledger_account_code": "1151",
                "ledger_account_name": "Ikeja GTBank ledger",
                "is_primary": True,
                "is_primary_collection": True,
            },
            {
                "branch": self.lekki,
                "opening_balance": lekki,
                "bank_account_name": "Lekki GTBank",
                "ledger_account_code": "1152",
                "ledger_account_name": "Lekki GTBank ledger",
                "is_primary": False,
                "is_primary_collection": True,
            },
        ]

    def body(self, **overrides):
        body = {
            "split_date": str(JAN_15),
            "agreement_reference": "BURSAR-MINUTES-2026-01-14",
            "allocations": [
                {**row, "branch": row["branch"].pk}
                for row in self.allocations()
            ],
        }
        body.update(overrides)
        return body

    def url(self, bank=None, books=None):
        return (
            f"/v1/finance/bank-accounts/{(bank or self.legacy_bank).pk}/split-by-branch/"
            f"?entity={(books or self.books).code}"
        )

    def test_service_splits_the_signed_balance_and_preserves_history(self):
        from .bank_splits import split_shared_bank_account

        result = split_shared_bank_account(
            self.legacy_bank,
            self.allocations(),
            split_date=JAN_15,
            agreement_reference="BURSAR-MINUTES-2026-01-14",
            actor_user=self.whole_user,
        )

        self.legacy_bank.refresh_from_db()
        self.legacy_ledger.refresh_from_db()
        self.assertFalse(self.legacy_bank.is_active)
        self.assertFalse(self.legacy_ledger.is_active)
        self.assertFalse(self.legacy_bank.is_primary)
        self.assertFalse(self.legacy_bank.is_primary_collection)
        self.assertEqual(net(self.legacy_ledger), 0)
        self.assertEqual(result.legacy_balance, 1_000_000)
        self.assertEqual(len(result.bank_accounts), 2)
        by_branch = {bank.branch_id: bank for bank in result.bank_accounts}
        self.assertEqual(net(by_branch[self.ikeja.pk].gl_account), 600_000)
        self.assertEqual(net(by_branch[self.lekki.pk].gl_account), 400_000)
        self.assertEqual(
            {entry.branch_id for entry in result.journals},
            {self.ikeja.pk, self.lekki.pk},
        )
        self.assertEqual(
            {entry.source for entry in result.journals},
            {JournalSource.SYSTEM},
        )
        self.assertEqual(len(result.journals), 3)
        ikeja_sheet = balance_sheet(
            self.books,
            as_of=JAN_15,
            scope=BranchScope(frozenset({self.ikeja.pk}), include_shared=False),
        )
        lekki_sheet = balance_sheet(
            self.books,
            as_of=JAN_15,
            scope=BranchScope(frozenset({self.lekki.pk}), include_shared=False),
        )
        self.assertTrue(ikeja_sheet.is_balanced, ikeja_sheet.difference)
        self.assertTrue(lekki_sheet.is_balanced, lekki_sheet.difference)
        self.assertEqual(ikeja_sheet.total_assets, 600_000)
        self.assertEqual(lekki_sheet.total_assets, 400_000)
        self.assertEqual(
            [(bank.account_number, bank.gateway_subaccount_code,
              bank.gateway_subaccount_provider, bank.settlement_bank_code)
             for bank in result.bank_accounts],
            [("0123456789", "", "", "058"), ("0123456789", "", "", "058")],
        )
        self.assertEqual(
            [(bank.is_primary, bank.is_primary_collection) for bank in result.bank_accounts],
            [(True, True), (False, True)],
        )
        self.assertEqual(self.statement.bank_account_id, self.legacy_bank.pk)
        self.assertTrue(
            JournalLine.objects.filter(
                entry=self.historical_journal,
                account=self.legacy_ledger,
                debit=1_000_000,
            ).exists()
        )
        audit = FinanceAuditLog.objects.filter(
            target_type="BankAccount",
            target_id=str(self.legacy_bank.pk),
            metadata__operation="SHARED_BANK_ACCOUNT_SPLIT",
        ).get()
        self.assertEqual(audit.actor_id, self.whole_user.pk)
        self.assertEqual(audit.metadata["agreement_reference"], "BURSAR-MINUTES-2026-01-14")
        self.assertEqual(audit.metadata["split_date"], "2026-01-15")
        self.assertEqual(audit.metadata["legacy_balance"], 1_000_000)
        self.assertEqual(
            audit.metadata["new_bank_account_ids"],
            [bank.pk for bank in result.bank_accounts],
        )
        self.assertEqual(audit.metadata["journal_ids"], [entry.pk for entry in result.journals])
        self.assertEqual(
            [row["opening_balance"] for row in audit.metadata["allocations"]],
            [600_000, 400_000],
        )

    def test_at_least_two_distinct_branches_and_an_agreement_are_required(self):
        from .bank_splits import split_shared_bank_account

        with self.assertRaisesMessage(BankAccountSplitError, "between 2 and"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations()[:1],
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        duplicate = self.allocations()
        duplicate[1]["branch"] = self.ikeja
        with self.assertRaisesMessage(BankAccountSplitError, "distinct branch"):
            split_shared_bank_account(
                self.legacy_bank,
                duplicate,
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        with self.assertRaisesMessage(BankAccountSplitError, "agreement reference"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="  ",
            )

    def test_only_an_active_unbranched_legacy_account_can_be_split(self):
        from .bank_splits import split_shared_bank_account

        BankAccount.objects.filter(pk=self.legacy_bank.pk).update(branch=self.ikeja)
        with self.assertRaisesMessage(BankAccountSplitError, "already belongs to a branch"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        BankAccount.objects.filter(pk=self.legacy_bank.pk).update(branch=None, is_active=False)
        with self.assertRaisesMessage(BankAccountSplitError, "already inactive"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )

    def test_successor_names_and_codes_must_be_unique_and_unused(self):
        from .bank_splits import split_shared_bank_account

        duplicate_name = self.allocations()
        duplicate_name[1]["bank_account_name"] = duplicate_name[0]["bank_account_name"]
        with self.assertRaisesMessage(BankAccountSplitError, "name must be unique"):
            split_shared_bank_account(
                self.legacy_bank,
                duplicate_name,
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        duplicate_code = self.allocations()
        duplicate_code[1]["ledger_account_code"] = duplicate_code[0]["ledger_account_code"]
        with self.assertRaisesMessage(BankAccountSplitError, "code must be unique"):
            split_shared_bank_account(
                self.legacy_bank,
                duplicate_code,
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        duplicate_ledger_name = self.allocations()
        duplicate_ledger_name[1]["ledger_account_name"] = (
            duplicate_ledger_name[0]["ledger_account_name"]
        )
        with self.assertRaisesMessage(BankAccountSplitError, "ledger account name must be unique"):
            split_shared_bank_account(
                self.legacy_bank,
                duplicate_ledger_name,
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )

        existing_ledger = Account.objects.create(
            entity=self.books, code="1159", name="Lekki GTBank ledger",
            account_type="ASSET", normal_balance="DEBIT", is_postable=True,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "ledger account name already exists"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        existing_ledger.name = "Existing ledger"
        existing_ledger.code = "1152"
        existing_ledger.save(update_fields=["name", "code"])
        with self.assertRaisesMessage(BankAccountSplitError, "code already exists"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        existing_ledger.code = "1159"
        existing_ledger.save(update_fields=["code"])
        BankAccount.objects.create(
            entity=self.books, branch=self.yaba, gl_account=existing_ledger,
            name="Lekki GTBank",
        )
        with self.assertRaisesMessage(BankAccountSplitError, "name already exists"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )

    def test_an_overdraft_splits_with_credits_in_the_new_ledgers(self):
        from .bank_splits import split_shared_bank_account

        self.post_movement(
            self.books, self.legacy_ledger, -1_250_000, JAN_10, branch=self.ikeja,
        )
        result = split_shared_bank_account(
            self.legacy_bank,
            self.allocations(ikeja=-100_000, lekki=-150_000),
            split_date=JAN_15,
            agreement_reference="BURSAR-OVERDRAFT-1",
            actor_user=self.whole_user,
        )

        self.assertEqual(result.legacy_balance, -250_000)
        balances = {bank.branch_id: net(bank.gl_account) for bank in result.bank_accounts}
        self.assertEqual(balances, {self.ikeja.pk: -100_000, self.lekki.pk: -150_000})
        self.assertEqual(net(self.legacy_ledger), 0)

    def test_the_agreed_total_must_equal_the_legacy_balance(self):
        from .bank_splits import split_shared_bank_account

        with self.assertRaisesMessage(BankAccountSplitError, "sum to 999999"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(ikeja=600_000, lekki=399_999),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

        self.assertFalse(BankAccount.objects.filter(entity=self.books, branch__isnull=False).exists())
        self.legacy_bank.refresh_from_db()
        self.assertTrue(self.legacy_bank.is_active)

    def test_future_posted_movement_blocks_the_cutover(self):
        from .bank_splits import split_shared_bank_account

        self.post_movement(
            self.books, self.legacy_ledger, 50_000, JAN_20, branch=self.ikeja,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "posted movement after"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

    def test_a_future_split_date_is_refused_before_the_source_is_retired(self):
        from .bank_splits import split_shared_bank_account

        with patch("vs_finance.bank_splits.tenant_today", return_value=JAN_15):
            with self.assertRaisesMessage(BankAccountSplitError, "current date"):
                split_shared_bank_account(
                    self.legacy_bank,
                    self.allocations(),
                    split_date=JAN_20,
                    agreement_reference="BURSAR-MINUTES-1",
                    actor_user=self.whole_user,
                )

        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)

    def test_statement_activity_must_be_complete_and_reconciled_at_cutover(self):
        from .bank_splits import split_shared_bank_account

        line = BankStatementLine.objects.create(
            bank_account=self.legacy_bank,
            txn_date=JAN_20,
            amount=25_000,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "statement activity after"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        line.delete()

        self.statement.statement_date = JAN_20
        self.statement.save(update_fields=["statement_date"])
        with self.assertRaisesMessage(BankAccountSplitError, "statement ending after"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )
        self.statement.statement_date = JAN_10
        self.statement.save(update_fields=["statement_date"])

        BankStatementLine.objects.create(
            bank_account=self.legacy_bank,
            statement=self.statement,
            txn_date=JAN_10,
            amount=25_000,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "unmatched line"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
            )

    def test_unplaced_historical_movement_must_be_backfilled_first(self):
        from .bank_splits import split_shared_bank_account

        JournalEntry.objects.filter(pk=self.historical_journal.pk).update(branch=None)
        with self.assertRaisesMessage(BankAccountSplitError, "branch backfill"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

    def test_a_failed_second_post_rolls_back_every_created_record(self):
        from . import bank_splits

        original_post = bank_splits.post_journal
        calls = 0

        def fail_second(entry, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BankAccountSplitError("Second branch failed.")
            return original_post(entry, **kwargs)

        with patch("vs_finance.bank_splits.post_journal", side_effect=fail_second):
            with self.assertRaisesMessage(BankAccountSplitError, "Second branch failed"):
                bank_splits.split_shared_bank_account(
                    self.legacy_bank,
                    self.allocations(),
                    split_date=JAN_15,
                    agreement_reference="BURSAR-MINUTES-1",
                    actor_user=self.whole_user,
                )

        self.assertFalse(BankAccount.objects.filter(entity=self.books, branch__isnull=False).exists())
        self.assertFalse(Account.objects.filter(entity=self.books, code__in=("1151", "1152")).exists())
        self.assertEqual(net(self.legacy_ledger), 1_000_000)

    def test_a_closed_period_rolls_back_the_whole_split(self):
        from .bank_splits import split_shared_bank_account

        period = resolve_period(self.books, JAN_15)
        period.status = PeriodStatus.SOFT_CLOSED
        period.save(update_fields=["status"])

        with self.assertRaises(PeriodClosedError):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )
        self.assertFalse(BankAccount.objects.filter(entity=self.books, branch__isnull=False).exists())

    def test_a_successful_split_cannot_be_retried(self):
        from .bank_splits import split_shared_bank_account

        split_shared_bank_account(
            self.legacy_bank,
            self.allocations(),
            split_date=JAN_15,
            agreement_reference="BURSAR-MINUTES-1",
            actor_user=self.whole_user,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "already inactive"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )
        self.assertEqual(BankAccount.objects.filter(entity=self.books, branch__isnull=False).count(), 2)

    def test_primary_flags_are_explicit_and_cannot_conflict(self):
        from .bank_splits import split_shared_bank_account

        allocations = self.allocations()
        allocations[1]["is_primary"] = True
        with self.assertRaisesMessage(BankAccountSplitError, "one successor"):
            split_shared_bank_account(
                self.legacy_bank,
                allocations,
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

        other_ledger = Account.objects.create(
            entity=self.books, code="1159", name="Existing collection ledger",
            account_type="ASSET", normal_balance="DEBIT", is_postable=True,
        )
        BankAccount.objects.create(
            entity=self.books, branch=self.lekki, gl_account=other_ledger,
            name="Existing Lekki Collection", is_primary_collection=True,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "already has a primary collection"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

    def test_a_successor_cannot_displace_an_existing_entity_primary(self):
        from .bank_splits import split_shared_bank_account

        other_ledger = Account.objects.create(
            entity=self.books, code="1159", name="Existing primary ledger",
            account_type="ASSET", normal_balance="DEBIT", is_postable=True,
        )
        BankAccount.objects.create(
            entity=self.books, branch=self.yaba, gl_account=other_ledger,
            name="Existing primary", is_primary=True,
        )
        with self.assertRaisesMessage(BankAccountSplitError, "already the entity primary"):
            split_shared_bank_account(
                self.legacy_bank,
                self.allocations(),
                split_date=JAN_15,
                agreement_reference="BURSAR-MINUTES-1",
                actor_user=self.whole_user,
            )

    def test_http_requires_both_primary_choices_on_every_allocation(self):
        body = self.body()
        del body["allocations"][0]["is_primary"]

        response = TenantAPIClient(user=self.whole_user).post(
            self.url(), body, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("is_primary", str(response.data))
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)

    def test_http_requires_whole_tenant_reach_and_the_update_permission(self):
        partial = TenantAPIClient(user=self.partial_user).post(
            self.url(), self.body(), format="json",
        )
        no_key = TenantAPIClient(user=self.user_for(
            self.tenant, "no-bank-split-key@fin.test",
        )).post(self.url(), self.body(), format="json")

        self.assertEqual(partial.status_code, 403, partial.data)
        self.assertIn("school-wide administrator", str(partial.data))
        self.assertEqual(no_key.status_code, 403, no_key.data)
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)

    def test_http_refuses_a_branch_from_another_tenant(self):
        body = self.body()
        body["allocations"][1]["branch"] = self.rival_branch.pk

        response = TenantAPIClient(user=self.whole_user).post(
            self.url(), body, format="json",
        )

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("does not belong to these books", str(response.data))
        self.assertFalse(BankAccount.objects.filter(entity=self.books, branch__isnull=False).exists())

    def test_http_returns_the_two_new_accounts(self):
        response = TenantAPIClient(user=self.whole_user).post(
            self.url(), self.body(), format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["legacy_balance"], 1_000_000)
        self.assertEqual(
            {row["branch_id"] for row in response.data["data"]["bank_accounts"]},
            {self.ikeja.pk, self.lekki.pk},
        )
        self.assertNotIn("account_number", response.data["data"]["bank_accounts"][0])
        self.assertEqual(len(response.data["data"]["journal_ids"]), 3)
