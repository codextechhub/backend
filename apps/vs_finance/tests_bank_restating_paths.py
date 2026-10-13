"""Payloads that restate a bank account's registered balances honour the same switches.

``finance.bankaccount`` registers the book balance, the statement balance and
(with both) the difference, and the ledger account behind the account. The
account's detail screen already filters them. Other payloads carry the same
numbers: a bank statement's opening and closing balances, a reconciliation run,
the chart of accounts row of the ledger account behind a bank account, that
ledger account's detail and activity, and the upload summary of a statement
import. Each is read here as a role that may open the screen but may not read
the figure, beside a role that may.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch; Rival Group is
another school.
"""
from __future__ import annotations

import datetime
import itertools

from vs_finance.banking import import_statement_lines
from vs_finance.models import Account, BankAccount, BankStatement
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import set_field_access

from .tests_bank_account_field_access import BANK, _BankAccountFieldAccess

_seq = itertools.count(1)
BANK_KEYS = (
    "finance.bankaccount.view", "finance.bankaccount.import", "finance.bankaccount.reconcile",
    "finance.account.view",
)


def _k(*names):
    return [f"{BANK}.{name}" for name in names]


class _BankRestating(_BankAccountFieldAccess):
    """Ikeja's account holds 7,000,000 of owner capital and has one uploaded statement."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.statement, _created, _held = import_statement_lines(
            cls.ikeja_bank,
            [{"txn_date": datetime.date(2026, 1, 20), "amount": 2_000,
              "description": "Deposit", "external_id": "TX-1"}],
            statement_date=datetime.date(2026, 1, 31), period_label="January 2026",
            opening_balance=1_000_000,
        )
        cls.solo_statement, _created, _held = import_statement_lines(
            cls.solo_bank,
            [{"txn_date": datetime.date(2026, 1, 20), "amount": 5_000,
              "description": "Fees", "external_id": "SOLO-1"}],
            statement_date=datetime.date(2026, 1, 31), opening_balance=100_000,
        )

    @classmethod
    def who(cls, *, hide=(), tenant=None, branch=None, keys=BANK_KEYS):
        """A user holding ``keys`` whose role has the named bank account fields switched off."""
        tenant = tenant or cls.tenant
        name = f"bankrestate-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@bank.test"), *keys,
            tenant=tenant, role_key=name, branch=branch,
        )
        if hide:
            role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
            set_field_access(role, *_k(*hide), read=False, write=False)
        return user

    def get(self, user, path, books=None):
        path, _, query = path.partition("?")
        return self.api(user).get(self.url(path, books) + (f"&{query}" if query else ""))

    def data(self, user, path, books=None):
        response = self.get(user, path, books)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class BankStatementAccessTests(_BankRestating):
    """A statement's balances are the bank's own figures behind ``statement_balance``."""

    NAMES = ("opening_balance", "opening_balance_naira", "closing_balance", "closing_balance_naira")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.who()
        cls.no_statement = cls.who(hide=("statement_balance",))
        cls.no_book = cls.who(hide=("book_balance",))
        cls.solo_open = cls.who(tenant=cls.solo_tenant)
        cls.solo_no_statement = cls.who(hide=("statement_balance",), tenant=cls.solo_tenant)
        cls.rival_open = cls.who(tenant=cls.rival_tenant)

    def listed(self, user):
        detail = self.data(user, f"bank-accounts/{self.ikeja_bank.pk}/")
        return next(row for row in detail["statements"] if row["id"] == self.statement.pk)

    def test_an_open_role_reads_the_balances_in_the_account_detail(self):
        row = self.listed(self.open)

        for name in self.NAMES:
            self.assertIn(name, row)
        self.assertEqual(row["closing_balance"], 1_002_000)

    def test_a_hidden_statement_balance_leaves_the_balances_in_the_account_detail(self):
        row = self.listed(self.no_statement)

        for name in self.NAMES:
            self.assertNotIn(name, row)
        self.assertEqual(row["period_label"], "January 2026")
        self.assertEqual(row["line_count"], 1)

    def test_the_book_balance_switch_does_not_hide_the_statements_balances(self):
        self.assertIn("closing_balance", self.listed(self.no_book))

    def test_the_statement_detail_follows_the_switch(self):
        path = f"bank-accounts/{self.ikeja_bank.pk}/statements/{self.statement.pk}/"
        hidden = self.data(self.no_statement, path)
        shown = self.data(self.open, path)

        for name in self.NAMES:
            self.assertNotIn(name, hidden)
            self.assertIn(name, shown)
        self.assertEqual(len(hidden["lines"]), 1)

    def correction(self, **extra):
        line = self.statement.lines.get()
        return {
            "statement_date": "2026-01-31", "period_label": "January 2026",
            "lines": [{"id": line.pk, "txn_date": "2026-01-20", "amount": 2_000,
                       "description": "Deposit", "external_id": "TX-1"}],
            **extra,
        }

    @property
    def statement_path(self):
        return f"bank-accounts/{self.ikeja_bank.pk}/statements/{self.statement.pk}/"

    def test_a_correction_without_an_opening_balance_keeps_the_one_on_file(self):
        body = self.correction()
        body["lines"][0]["amount"] = 3_000

        response = self.api(self.no_statement).patch(
            self.url(self.statement_path), body, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        for name in self.NAMES:
            self.assertNotIn(name, response.data["data"])
        self.assertEqual(response.data["data"]["lines"][0]["amount"], 3_000)
        stored = BankStatement.objects.get(pk=self.statement.pk)
        self.assertEqual(stored.opening_balance, 1_000_000)
        self.assertEqual(stored.closing_balance, 1_003_000)

    def test_a_reader_of_the_statement_balance_may_correct_the_opening_balance(self):
        response = self.api(self.open).patch(
            self.url(self.statement_path), self.correction(opening_balance=900_000), format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["opening_balance"], 900_000)
        self.assertEqual(
            BankStatement.objects.get(pk=self.statement.pk).closing_balance, 902_000)

    def test_a_caller_who_cannot_read_the_balance_may_not_write_it_whatever_the_value(self):
        for value in (900_000, 1_000_000):
            response = self.api(self.no_statement).patch(
                self.url(self.statement_path), self.correction(opening_balance=value),
                format="json")

            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(response.data["error"]["code"], "field_write_denied")
            self.assertIn("opening_balance", response.data["error"]["detail"])
            stored = BankStatement.objects.get(pk=self.statement.pk)
            self.assertEqual((stored.opening_balance, stored.closing_balance),
                             (1_000_000, 1_002_000))

    def test_the_book_balance_switch_does_not_stop_a_correction_of_the_opening_balance(self):
        response = self.api(self.no_book).patch(
            self.url(self.statement_path), self.correction(opening_balance=950_000), format="json")

        self.assertEqual(response.status_code, 200, response.data)

    def test_a_single_branch_school_follows_the_same_rule(self):
        path = f"bank-accounts/{self.solo_bank.pk}/statements/{self.solo_statement.pk}/"
        line = self.solo_statement.lines.get()
        body = {
            "statement_date": "2026-01-31",
            "lines": [{"id": line.pk, "txn_date": "2026-01-20", "amount": 5_000,
                       "description": "Fees", "external_id": "SOLO-1"}],
        }
        url = self.url(path, self.solo_books)

        refused = self.api(self.solo_no_statement).patch(
            url, {**body, "opening_balance": 100_000}, format="json")
        kept = self.api(self.solo_no_statement).patch(url, body, format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(kept.status_code, 200, kept.data)
        self.assertEqual(BankStatement.objects.get(pk=self.solo_statement.pk).opening_balance, 100_000)

    def test_the_correction_entry_hides_the_balances_from_a_caller_who_cannot_read_them(self):
        self.api(self.open).patch(
            self.url(self.statement_path), self.correction(opening_balance=900_000), format="json")
        auditor = self.who(keys=(*BANK_KEYS, "finance.audit.view"))
        blind_auditor = self.who(hide=("statement_balance",), keys=(*BANK_KEYS, "finance.audit.view"))

        def entry(user):
            rows = self.data(user, "audit-logs/?action=BANK_STATEMENT_CORRECTED")
            return rows[0]

        shown = entry(auditor)
        hidden = entry(blind_auditor)

        self.assertEqual(shown["after"]["opening_balance"], 900_000)
        self.assertEqual(shown["before"]["closing_balance"], 1_002_000)
        for snapshot in (hidden["before"], hidden["after"]):
            self.assertNotIn("opening_balance", snapshot)
            self.assertNotIn("closing_balance", snapshot)
            self.assertEqual(snapshot["period_label"], "January 2026")
            self.assertEqual(snapshot["lines"][0]["amount"], 2_000)

    def test_a_single_branch_school_follows_the_same_switch(self):
        path = f"bank-accounts/{self.solo_bank.pk}/statements/{self.solo_statement.pk}/"

        hidden = self.data(self.solo_no_statement, path, self.solo_books)
        shown = self.data(self.solo_open, path, self.solo_books)

        self.assertNotIn("closing_balance", hidden)
        self.assertEqual(shown["closing_balance"], 105_000)

    def test_one_schools_role_never_reads_another_schools_statement(self):
        path = f"bank-accounts/{self.ikeja_bank.pk}/statements/{self.statement.pk}/"

        self.assertEqual(self.get(self.rival_open, path, self.rival_books).status_code, 404)
        self.assertIn(self.get(self.rival_open, path).status_code, (403, 404))


class BankReconciliationAccessTests(_BankRestating):
    """A reconciliation run restates the two balances, and the difference needs both."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.who()
        cls.no_statement = cls.who(hide=("statement_balance",))
        cls.no_book = cls.who(hide=("book_balance",))
        cls.solo_no_statement = cls.who(hide=("statement_balance",), tenant=cls.solo_tenant)

    def complete(self, user, bank=None, books=None):
        bank = bank or self.ikeja_bank
        response = self.api(user).post(
            self.url(f"bank-accounts/{bank.pk}/reconcile/complete/", books), {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]

    def test_an_open_role_reads_the_run(self):
        data = self.complete(self.open)

        for name in ("book_balance", "statement_balance", "difference", "difference_naira"):
            self.assertIn(name, data)

    def test_a_hidden_statement_balance_takes_the_balance_and_the_difference(self):
        data = self.complete(self.no_statement)

        self.assertNotIn("statement_balance", data)
        self.assertNotIn("statement_balance_naira", data)
        self.assertNotIn("difference", data)
        self.assertNotIn("difference_naira", data)
        self.assertIn("book_balance", data)
        self.assertIn("matched_count", data)

    def test_a_hidden_book_balance_takes_the_balance_and_the_difference(self):
        data = self.complete(self.no_book)

        self.assertNotIn("book_balance", data)
        self.assertNotIn("difference", data)
        self.assertIn("statement_balance", data)

    def test_the_runs_in_the_account_detail_follow_the_same_rule(self):
        self.complete(self.open)
        runs = self.data(self.no_statement, f"bank-accounts/{self.ikeja_bank.pk}/")["reconciliations"]

        self.assertTrue(runs)
        for run in runs:
            self.assertNotIn("statement_balance", run)
            self.assertNotIn("difference", run)

    def test_a_single_branch_school_follows_the_same_switch(self):
        data = self.complete(self.solo_no_statement, self.solo_bank, self.solo_books)

        self.assertNotIn("difference", data)
        self.assertNotIn("statement_balance", data)


class BankLedgerAccountAccessTests(_BankRestating):
    """The ledger account behind a bank account carries the account's book balance as its own."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.who()
        cls.no_book = cls.who(hide=("book_balance",))
        cls.no_link = cls.who(hide=("ledger_account",))
        cls.ikeja_no_book = cls.who(hide=("book_balance",), branch=cls.ikeja)
        cls.solo_no_book = cls.who(hide=("book_balance",), tenant=cls.solo_tenant)
        cls.solo_open = cls.who(tenant=cls.solo_tenant)
        cls.capital = cls.acc("3100")

    def chart(self, user, books=None):
        rows = self.data(user, "accounts/?with_balance=true", books)
        return {row["code"]: row for row in rows}

    def test_an_open_role_reads_the_link_and_the_balance(self):
        chart = self.chart(self.open)

        self.assertEqual(chart["1151"]["bank_account_id"], self.ikeja_bank.pk)
        self.assertEqual(chart["1151"]["balance"]["kobo"], 7_000_000)

    def test_a_hidden_ledger_account_removes_the_link_from_the_chart(self):
        chart = self.chart(self.no_link)

        self.assertNotIn("bank_account_id", chart["1151"])
        self.assertIn("balance", chart["1151"])
        self.assertEqual(chart["1151"]["code"], "1151")

    def test_a_hidden_book_balance_removes_the_balance_of_a_bank_ledger_account_only(self):
        chart = self.chart(self.no_book)

        self.assertNotIn("balance", chart["1151"])
        self.assertIn("bank_account_id", chart["1151"])
        self.assertEqual(chart["3100"]["balance"]["kobo"], 7_000_000)

    def test_a_branch_bound_reader_follows_the_same_switch(self):
        chart = self.chart(self.ikeja_no_book)

        self.assertNotIn("balance", chart["1151"])
        self.assertIn("balance", chart["3100"])

    def test_the_account_detail_drops_its_balances_and_running_balance(self):
        path = f"accounts/{self.ikeja_bank.gl_account_id}/"
        shown = self.data(self.open, path)
        hidden = self.data(self.no_book, path)

        self.assertIn("current_balance", shown["summary"])
        self.assertIn("opening_balance", shown["summary"])
        self.assertIn("running_balance", shown["activity"][0])
        self.assertNotIn("current_balance", hidden["summary"])
        self.assertNotIn("opening_balance", hidden["summary"])
        self.assertEqual(hidden["summary"]["line_count"], shown["summary"]["line_count"])
        self.assertTrue(hidden["activity"])
        for row in hidden["activity"]:
            self.assertNotIn("running_balance", row)
            self.assertIn("debit", row)
        self.assertNotIn("bank_account_id", self.data(self.no_link, path)["account"])

    def test_an_ordinary_account_detail_keeps_its_balances(self):
        data = self.data(self.no_book, f"accounts/{self.capital.pk}/")

        self.assertIn("current_balance", data["summary"])
        self.assertIn("running_balance", data["activity"][0])

    def test_the_activity_totals_follow_the_book_balance_switch(self):
        bank_path = f"accounts/{self.ikeja_bank.gl_account_id}/activity/"
        capital_path = f"accounts/{self.capital.pk}/activity/"

        shown = self.get(self.open, bank_path).data
        hidden = self.get(self.no_book, bank_path).data
        ordinary = self.get(self.no_book, capital_path).data

        self.assertIn("totals", shown)
        self.assertNotIn("totals", hidden)
        self.assertTrue(hidden["data"])
        self.assertIn("totals", ordinary)

    def group_over_the_bank(self, books, *bank_accounts):
        """A header account over *bank_accounts*' ledger accounts, as the chart can hold one."""
        group = Account.objects.create(
            entity=books, code="1190", name="Banks", account_type=bank_accounts[0].gl_account.account_type,
            is_postable=False,
        )
        Account.objects.filter(pk__in=[b.gl_account_id for b in bank_accounts]).update(parent=group)
        return group

    def test_a_group_over_a_bank_ledger_account_follows_the_book_balance_switch(self):
        group = self.group_over_the_bank(self.books, self.ikeja_bank, self.lekki_bank)
        path = f"accounts/{group.pk}/"

        shown = self.data(self.open, path)["summary"]
        hidden = self.data(self.no_book, path)["summary"]
        branch_bound = self.data(self.ikeja_no_book, path)["summary"]

        self.assertEqual(shown["current_balance"]["kobo"], 7_000_000)
        self.assertIn("opening_balance", shown)
        for summary in (hidden, branch_bound):
            self.assertNotIn("current_balance", summary)
            self.assertNotIn("opening_balance", summary)
            self.assertEqual(summary["line_count"], shown["line_count"])

    def test_one_bank_ledger_account_among_ordinary_ones_is_enough_to_withhold_the_group(self):
        group = self.group_over_the_bank(self.books, self.ikeja_bank)
        Account.objects.filter(pk=self.capital.pk).update(parent=group)

        summary = self.data(self.no_book, f"accounts/{group.pk}/")["summary"]

        self.assertNotIn("current_balance", summary)
        self.assertNotIn("opening_balance", summary)

    def test_a_group_with_no_bank_ledger_account_keeps_its_balances(self):
        group = Account.objects.create(
            entity=self.books, code="3190", name="Equity group",
            account_type=self.capital.account_type, is_postable=False,
        )
        Account.objects.filter(pk=self.capital.pk).update(parent=group)

        data = self.data(self.no_book, f"accounts/{group.pk}/")["summary"]
        totals = self.get(self.no_book, f"accounts/{group.pk}/activity/").data

        self.assertEqual(data["current_balance"]["kobo"], 7_000_000)
        self.assertIn("totals", totals)

    def test_the_activity_totals_of_a_group_over_a_bank_follow_the_switch(self):
        group = self.group_over_the_bank(self.books, self.ikeja_bank)
        path = f"accounts/{group.pk}/activity/"

        shown = self.get(self.open, path).data
        hidden = self.get(self.no_book, path).data

        self.assertIn("totals", shown)
        self.assertNotIn("totals", hidden)
        self.assertTrue(hidden["data"])

    def test_picking_the_bank_out_of_a_group_does_not_bring_the_totals_back(self):
        group = self.group_over_the_bank(self.books, self.ikeja_bank)
        Account.objects.filter(pk=self.capital.pk).update(parent=group)
        path = f"accounts/{group.pk}/activity/"

        picked_bank = self.get(self.no_book, f"{path}?account={self.ikeja_bank.gl_account_id}").data
        picked_other = self.get(self.no_book, f"{path}?account={self.capital.pk}").data

        self.assertNotIn("totals", picked_bank)
        self.assertEqual(picked_other["totals"]["credit"]["kobo"], 7_000_000)

    def test_a_group_in_a_single_branch_school_follows_the_same_switch(self):
        group = self.group_over_the_bank(self.solo_books, self.solo_bank)

        self.assertNotIn(
            "current_balance",
            self.data(self.solo_no_book, f"accounts/{group.pk}/", self.solo_books)["summary"],
        )
        self.assertIn(
            "current_balance",
            self.data(self.solo_open, f"accounts/{group.pk}/", self.solo_books)["summary"],
        )

    def test_a_single_branch_school_follows_the_same_switch(self):
        hidden = self.chart(self.solo_no_book, self.solo_books)
        shown = self.chart(self.solo_open, self.solo_books)

        self.assertNotIn("balance", hidden["1151"])
        self.assertIn("balance", shown["1151"])

    def test_a_school_cannot_read_another_schools_chart(self):
        response = self.get(self.no_book, "accounts/?with_balance=true", self.rival_books)

        self.assertIn(response.status_code, (403, 404))
