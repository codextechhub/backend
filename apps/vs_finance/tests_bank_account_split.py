"""A shared physical bank account becomes one branch-owned ledger per branch.

Bright Star's old GTBank record predates branch books. Its statements and posted
journals remain where they were, while an agreed cutover moves the exact signed
balance into one new bank ledger for Ikeja and one for Lekki. The old account is
then inactive and cannot be split a second time.

Where a branch's own entries on the old account differ from the share it agrees
to take, the difference is by default a debt between branches, booked as a
``BANK_SPLIT`` inter-branch transfer; the person splitting may instead move it
permanently through retained earnings.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_rbac.scoping import BranchScope

from .account_mappings import resolve_mapped_account
from .bank_splits import match_differences
from .branch_ledger import ledger_lines
from .constants import (
    AccountMappingKey,
    BankSplitDifferenceTreatment,
    DocumentStatus,
    InterBranchTransferKind,
    JournalSource,
    PeriodStatus,
)
from .exceptions import BankAccountSplitError, InterBranchError, PeriodClosedError
from .inter_branch import inter_branch_close_check, pair_balances, void_inter_branch_transfer
from .models import (
    Account,
    BankAccount,
    BankStatement,
    BankStatementLine,
    BranchFiscalPeriod,
    FinanceAuditLog,
    InterBranchTransfer,
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

    def retained_earnings(self):
        return resolve_mapped_account(self.books, AccountMappingKey.RETAINED_EARNINGS)

    def url(self, bank=None, books=None):
        return (
            f"/v1/finance/bank-accounts/{(bank or self.legacy_bank).pk}/split-by-branch/"
            f"?entity={(books or self.books).code}"
        )

    def test_a_permanent_move_splits_through_retained_earnings_and_preserves_history(self):
        """Ikeja's entries hold all N10,000; Ikeja takes N6,000 and Lekki N4,000.

        Chosen as a permanent move, Ikeja's N10,000 is cleared against its
        retained earnings and each new account opened against its own, so Lekki
        keeps its N4,000 as equity and owes Ikeja nothing.
        """
        from .bank_splits import split_shared_bank_account

        result = split_shared_bank_account(
            self.legacy_bank,
            self.allocations(),
            split_date=JAN_15,
            agreement_reference="BURSAR-MINUTES-2026-01-14",
            difference_treatment=BankSplitDifferenceTreatment.PERMANENT_MOVE,
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
        self.assertEqual(audit.metadata["difference_treatment"], "PERMANENT_MOVE")
        self.assertEqual(audit.metadata["inter_branch_transfer_ids"], [])
        self.assertEqual(result.transfers, ())
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.books).exists())
        self.assertTrue(JournalLine.objects.filter(
            entry__in=result.journals, account=self.retained_earnings(),
        ).exists())
        self.assertTrue(all(
            "moved permanently through retained earnings" in entry.narration
            for entry in result.journals
        ))
        self.assertEqual(pair_balances(self.books)["pairs"], [])

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

        with self.assertRaisesMessage(BankAccountSplitError, "sum to ₦9,999.99"):
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
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.books).exists())
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
        self.assertEqual(response.data["data"]["difference_treatment"], "DEBT")
        self.assertEqual(
            [(row["from_branch_id"], row["to_branch_id"], row["amount"])
             for row in response.data["data"]["inter_branch_transfers"]],
            [(self.ikeja.pk, self.lekki.pk, 400_000)],
        )
        self.assertEqual(len(response.data["data"]["journal_ids"]), 4)

    def test_http_accepts_a_permanent_move_and_refuses_an_unknown_treatment(self):
        unknown = TenantAPIClient(user=self.whole_user).post(
            self.url(), self.body(difference_treatment="WRITE_OFF"), format="json",
        )
        self.assertEqual(unknown.status_code, 400, unknown.data)
        self.assertIn("difference_treatment", str(unknown.data))
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)

        response = TenantAPIClient(user=self.whole_user).post(
            self.url(), self.body(difference_treatment="PERMANENT_MOVE"), format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["difference_treatment"], "PERMANENT_MOVE")
        self.assertEqual(response.data["data"]["inter_branch_transfers"], [])
        self.assertEqual(len(response.data["data"]["journal_ids"]), 3)

    # -- what happens to a branch's difference ------------------------------ #

    def split(self, allocations, **kwargs):
        from .bank_splits import split_shared_bank_account

        kwargs.setdefault("agreement_reference", "BURSAR-MINUTES-2026-01-14")
        return split_shared_bank_account(
            self.legacy_bank, allocations, split_date=JAN_15,
            actor_user=self.whole_user, **kwargs,
        )

    def three_way(self, *, ikeja, lekki, yaba):
        rows = self.allocations(ikeja=ikeja, lekki=lekki)
        rows.append({
            "branch": self.yaba,
            "opening_balance": yaba,
            "bank_account_name": "Yaba GTBank",
            "ledger_account_code": "1153",
            "ledger_account_name": "Yaba GTBank ledger",
            "is_primary": False,
            "is_primary_collection": True,
        })
        return rows

    def owed(self):
        """Every pair balance as ``(owed_by, owed_to, amount, both sides agree)``."""
        return sorted(
            (pair["owed_by"]["id"], pair["owed_to"]["id"], pair["amount"], pair["balanced"])
            for pair in pair_balances(self.books)["pairs"]
        )

    def assert_books_still_close(self, result):
        """The month can close, every branch balances, and no equity moved."""
        check = inter_branch_close_check(self.books, resolve_period(self.books, JAN_15))
        self.assertTrue(check.passed, check.detail)
        self.assertEqual(pair_balances(self.books)["net_total"], 0)
        for branch in (self.ikeja, self.lekki, self.yaba):
            sheet = balance_sheet(
                self.books,
                as_of=JAN_15,
                scope=BranchScope(frozenset({branch.pk}), include_shared=False),
            )
            self.assertTrue(sheet.is_balanced, (branch.name, sheet.difference))
        self.assertFalse(JournalLine.objects.filter(
            entry__in=result.journals, account=self.retained_earnings(),
        ).exists())
        self.assertEqual(net(self.legacy_ledger), 0)

    def new_balances(self, result):
        return {bank.branch_id: net(bank.gl_account) for bank in result.bank_accounts}

    def test_a_difference_is_a_debt_between_two_branches_by_default(self):
        """The shared GTBank account holds N400,000.

        Ikeja's entries on it total N500,000 and Lekki's minus N100,000. The
        bursars agree Ikeja takes N250,000 and Lekki N150,000, so Lekki keeps
        N250,000 that Ikeja's entries put there. Lekki owes Ikeja N250,000 on the
        pair balances, and neither branch's retained earnings move.
        """
        self.post_movement(self.books, self.legacy_ledger, 49_000_000, JAN_10, branch=self.ikeja)
        self.post_movement(self.books, self.legacy_ledger, -10_000_000, JAN_10, branch=self.lekki)

        result = self.split(self.allocations(ikeja=25_000_000, lekki=15_000_000))

        self.assertEqual(result.difference_treatment, BankSplitDifferenceTreatment.DEBT)
        self.assertEqual(
            self.new_balances(result),
            {self.ikeja.pk: 25_000_000, self.lekki.pk: 15_000_000},
        )
        self.assertEqual(self.owed(), [(self.lekki.pk, self.ikeja.pk, 25_000_000, True)])
        self.assert_books_still_close(result)

        (transfer,) = result.transfers
        self.assertEqual(
            (transfer.kind, transfer.status, transfer.branch_id, transfer.to_branch_id,
             transfer.amount, transfer.transfer_date, transfer.reference),
            (InterBranchTransferKind.BANK_SPLIT, DocumentStatus.POSTED, self.ikeja.pk,
             self.lekki.pk, 25_000_000, JAN_15, "BURSAR-MINUTES-2026-01-14"),
        )
        self.assertEqual(
            {(leg.branch_id, leg.journal.branch_id) for leg in transfer.legs.all()},
            {(self.ikeja.pk, self.ikeja.pk), (self.lekki.pk, self.lekki.pk)},
        )
        self.assertTrue(JournalLine.objects.filter(
            entry__in=result.journals, entry__branch=self.ikeja,
            account__code="1260", counterparty_branch=self.lekki, debit=25_000_000,
        ).exists())
        self.assertTrue(all("debt" in entry.narration for entry in result.journals))

        audit = FinanceAuditLog.objects.get(
            target_type="BankAccount",
            target_id=str(self.legacy_bank.pk),
            metadata__operation="SHARED_BANK_ACCOUNT_SPLIT",
        )
        self.assertEqual(audit.metadata["difference_treatment"], "DEBT")
        self.assertIsNone(audit.metadata["retained_earnings_account_id"])
        self.assertEqual(audit.metadata["inter_branch_transfer_ids"], [transfer.pk])
        self.assertEqual(audit.metadata["journal_ids"], [entry.pk for entry in result.journals])
        self.assertEqual(
            [(row["branch_id"], row["book_balance"], row["agreed_balance"], row["difference"])
             for row in audit.metadata["branch_differences"]],
            [(self.ikeja.pk, 50_000_000, 25_000_000, 25_000_000),
             (self.lekki.pk, -10_000_000, 15_000_000, -25_000_000)],
        )

    def test_three_branches_settle_the_largest_deficit_against_the_largest_surplus_first(self):
        """Ikeja's entries hold N5,000 and Lekki's N4,000; Yaba's hold nothing.

        Each takes N3,000 of the N9,000. Ikeja is N2,000 over its share and
        Lekki N1,000 over; Yaba is N3,000 under. Yaba's deficit meets the
        largest surplus first, Ikeja's, and what is left of it Lekki's: Yaba owes
        Ikeja N2,000 and Lekki N1,000, and Ikeja and Lekki owe each other
        nothing. Every pair is explicit, both its sides agree, and the month's
        inter-branch close check passes.
        """
        self.post_movement(self.books, self.legacy_ledger, -500_000, JAN_10, branch=self.ikeja)
        self.post_movement(self.books, self.legacy_ledger, 400_000, JAN_10, branch=self.lekki)

        result = self.split(self.three_way(ikeja=300_000, lekki=300_000, yaba=300_000))

        self.assertEqual(
            [(t.branch_id, t.to_branch_id, t.amount) for t in result.transfers],
            [(self.ikeja.pk, self.yaba.pk, 200_000), (self.lekki.pk, self.yaba.pk, 100_000)],
        )
        self.assertEqual(self.owed(), sorted([
            (self.yaba.pk, self.ikeja.pk, 200_000, True),
            (self.yaba.pk, self.lekki.pk, 100_000, True),
        ]))
        self.assertEqual(
            self.new_balances(result),
            {self.ikeja.pk: 300_000, self.lekki.pk: 300_000, self.yaba.pk: 300_000},
        )
        self.assert_books_still_close(result)

    def test_a_branch_with_history_but_no_new_account_is_paid_out_as_a_debt(self):
        """Yaba's entries hold N2,000 of the N12,000, but only Ikeja and Lekki take accounts.

        Ikeja (N10,000 of entries) takes N6,000 and Lekki (none) N6,000. Lekki's
        N6,000 deficit meets Ikeja's N4,000 surplus and then Yaba's N2,000, so
        Lekki owes Ikeja N4,000 and Yaba N2,000, and Yaba's side of the old
        account is left at nothing.
        """
        self.post_movement(self.books, self.legacy_ledger, 200_000, JAN_10, branch=self.yaba)

        result = self.split(self.allocations(ikeja=600_000, lekki=600_000))

        self.assertEqual(self.owed(), sorted([
            (self.lekki.pk, self.ikeja.pk, 400_000, True),
            (self.lekki.pk, self.yaba.pk, 200_000, True),
        ]))
        self.assertEqual(
            self.new_balances(result), {self.ikeja.pk: 600_000, self.lekki.pk: 600_000},
        )
        yaba_side = ledger_lines(self.books).filter(
            account=self.legacy_ledger, entry__branch=self.yaba,
        )
        self.assertEqual(sum(line.debit - line.credit for line in yaba_side), 0)
        self.assert_books_still_close(result)

    def test_an_overdraft_difference_is_a_debt_too(self):
        """Ikeja ran the shared overdraft to N2,500; Ikeja keeps N1,000 of it and Lekki N1,500.

        Lekki takes on N1,500 of overdraft Ikeja's spending made, so Ikeja owes
        Lekki N1,500.
        """
        self.post_movement(self.books, self.legacy_ledger, -1_250_000, JAN_10, branch=self.ikeja)

        result = self.split(self.allocations(ikeja=-100_000, lekki=-150_000))

        self.assertEqual(
            self.new_balances(result), {self.ikeja.pk: -100_000, self.lekki.pk: -150_000},
        )
        self.assertEqual(self.owed(), [(self.ikeja.pk, self.lekki.pk, 150_000, True)])
        self.assert_books_still_close(result)

    def assert_equal_shares_post_no_difference(self, treatment):
        self.post_movement(self.books, self.legacy_ledger, -400_000, JAN_10, branch=self.ikeja)
        self.post_movement(self.books, self.legacy_ledger, 400_000, JAN_10, branch=self.lekki)

        result = self.split(self.allocations(ikeja=600_000, lekki=400_000),
                            difference_treatment=treatment)

        self.assertEqual(result.transfers, ())
        self.assertEqual(len(result.journals), 2)
        self.assertEqual(
            self.new_balances(result), {self.ikeja.pk: 600_000, self.lekki.pk: 400_000},
        )
        self.assertEqual(self.owed(), [])
        self.assertFalse(JournalLine.objects.filter(
            entry__in=result.journals, account__code="1260",
        ).exists())
        self.assertTrue(all("no difference" in entry.narration for entry in result.journals))
        self.assert_books_still_close(result)

    def test_equal_shares_post_no_difference_when_debt_is_chosen(self):
        self.assert_equal_shares_post_no_difference(BankSplitDifferenceTreatment.DEBT)

    def test_equal_shares_post_no_difference_when_a_permanent_move_is_chosen(self):
        self.assert_equal_shares_post_no_difference(BankSplitDifferenceTreatment.PERMANENT_MOVE)

    def test_an_unknown_treatment_is_refused(self):
        with self.assertRaisesMessage(BankAccountSplitError, "Choose how"):
            self.split(self.allocations(), difference_treatment="WRITE_OFF")
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)

    def test_a_named_branch_closed_on_its_own_refuses_the_split_under_either_treatment(self):
        BranchFiscalPeriod.objects.create(
            period=resolve_period(self.books, JAN_15), branch=self.lekki,
            status=PeriodStatus.SOFT_CLOSED,
        )
        for treatment in BankSplitDifferenceTreatment.values:
            with self.subTest(treatment=treatment):
                with self.assertRaises(PeriodClosedError):
                    self.split(self.allocations(), difference_treatment=treatment)
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)
        self.assertFalse(BankAccount.objects.filter(entity=self.books, branch__isnull=False).exists())
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.books).exists())

    def test_a_branch_the_split_does_not_name_may_be_closed(self):
        BranchFiscalPeriod.objects.create(
            period=resolve_period(self.books, JAN_15), branch=self.yaba,
            status=PeriodStatus.CLOSED,
        )

        result = self.split(self.allocations())

        self.assertEqual(len(result.transfers), 1)

    def test_a_split_difference_is_never_voided_and_the_split_is_not_retried(self):
        result = self.split(self.allocations())
        (transfer,) = result.transfers

        with self.assertRaisesMessage(InterBranchError, "cash transfer the other way"):
            void_inter_branch_transfer(transfer, actor_user=self.whole_user)
        with self.assertRaisesMessage(BankAccountSplitError, "already inactive"):
            self.split(self.allocations())

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, DocumentStatus.POSTED)
        self.assertEqual(InterBranchTransfer.objects.filter(entity=self.books).count(), 1)
        self.assertEqual(self.owed(), [(self.lekki.pk, self.ikeja.pk, 400_000, True)])

    def test_a_one_branch_tenant_has_no_second_branch_to_split_into(self):
        """Single Site's school-wide account already is its only branch's, so nothing splits."""
        from .bank_splits import split_shared_bank_account

        ledger = Account.objects.create(
            entity=self.solo_books,
            parent=Account.objects.get(entity=self.solo_books, code="1100"),
            code="1150", name="Main GTBank ledger", account_type="ASSET",
            normal_balance="DEBIT", is_postable=True,
        )
        bank = BankAccount.objects.create(
            entity=self.solo_books, branch=None, gl_account=ledger, name="Main GTBank",
        )
        self.post_movement(self.solo_books, ledger, 500_000, JAN_10, branch=self.solo_main)
        row = {
            "branch": self.solo_main,
            "opening_balance": 500_000,
            "bank_account_name": "Main branch GTBank",
            "ledger_account_code": "1151",
            "ledger_account_name": "Main branch GTBank ledger",
            "is_primary": False,
            "is_primary_collection": False,
        }

        with self.assertRaisesMessage(BankAccountSplitError, "between 2 and"):
            split_shared_bank_account(
                bank, [row], split_date=JAN_15, agreement_reference="SOLO-1",
            )
        with self.assertRaisesMessage(BankAccountSplitError, "distinct branch"):
            split_shared_bank_account(
                bank, [row, {**row, "opening_balance": 0}], split_date=JAN_15,
                agreement_reference="SOLO-1",
            )
        self.assertTrue(BankAccount.objects.get(pk=bank.pk).is_active)
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.solo_books).exists())


class MatchDifferencesTests(SimpleTestCase):
    """How branch differences are paired into debts, without touching the database."""

    def test_the_largest_deficit_settles_against_the_largest_surplus_first(self):
        """Four branches: +N5,000, +N3,000, -N4,000 and -N4,000.

        Branch 3 (the lower id of the two equal deficits) owes branch 1 N4,000,
        leaving branch 1 N1,000 over. Branch 4's N4,000 then meets the largest
        surplus left, branch 2's N3,000, and its last N1,000 branch 1's.
        """
        self.assertEqual(
            match_differences({1: 5000, 2: 3000, 3: -4000, 4: -4000}),
            [(1, 3, 4000), (2, 4, 3000), (1, 4, 1000)],
        )

    def test_the_pairing_does_not_depend_on_the_order_branches_are_given(self):
        forward = {1: 5000, 2: 3000, 3: -4000, 4: -4000}
        backward = dict(reversed(list(forward.items())))

        self.assertEqual(match_differences(backward), match_differences(forward))

    def test_no_difference_is_no_debt(self):
        self.assertEqual(match_differences({1: 0, 2: 0}), [])

    def test_differences_that_do_not_cancel_are_refused(self):
        with self.assertRaisesMessage(BankAccountSplitError, "do not cancel"):
            match_differences({1: 5000, 2: -4000})
