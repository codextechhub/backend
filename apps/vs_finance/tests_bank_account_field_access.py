"""Field Access on the bank account master (``finance.bankaccount``).

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch; Rival Group is a
third school. A bank account's name identifies it wherever it is picked, printed
or named in a message and carries no switch, so every test below expects it to
stay. Every other field of the account has a Read and a Write switch; the
figures the books work out (balances, reconciliation progress, recent activity)
are read-only.

The branch split of a shared account has its own tests beside the other bank
documents (:mod:`vs_finance.tests_bank_documents_field_access`).
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient
from vs_finance.models import BankAccount, BankReconciliation, BankStatementLine, FinanceAuditLog
from vs_finance.constants import FinanceAuditAction
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_bank_document_rework import _ReworkFixture

BANK = "finance.bankaccount"
_seq = itertools.count(1)

KEYS = (
    "finance.bankaccount.view", "finance.bankaccount.create", "finance.bankaccount.update",
    "finance.bankaccount.import", "finance.bankaccount.reconcile", "finance.audit.view",
)

SETUP = ("currency", "ledger_account", "is_active", "is_primary",
         "is_primary_collection")
FIGURES = ("book_balance", "statement_balance", "reconciliation", "recent_activity")


def _k(*names):
    return [f"{BANK}.{name}" for name in names]


class _BankAccountFieldAccess(_ReworkFixture):
    """The rework books, plus helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(BANK)
        BankAccount.objects.filter(pk=cls.ikeja_bank.pk).update(
            bank_name="Guaranty Trust Bank", account_number="0123456789",
            settlement_bank_code="058", currency_id=None,
        )
        BankAccount.objects.filter(pk=cls.solo_bank.pk).update(
            bank_name="Zenith Bank", account_number="2200000001")
        BankReconciliation.objects.create(
            bank_account=cls.ikeja_bank, as_of_date=datetime.date(2026, 1, 31),
            book_balance=5_000, statement_balance=4_000, difference=1_000, matched_count=2,
        )
        cls.posted_transaction()
        BankStatementLine.objects.create(
            bank_account=cls.ikeja_bank, txn_date=datetime.date(2026, 1, 20),
            description="Deposit", amount=2_000, status="UNMATCHED",
        )

    @classmethod
    def posted_transaction(cls):
        from vs_finance.banking import post_bank_transaction
        from vs_finance.models import BankTransaction

        txn = BankTransaction.objects.create(
            entity=cls.books, branch=cls.ikeja, bank_account=cls.ikeja_bank, direction="IN",
            amount=7_000_000, counter_account=cls.acc("3100"),
            transaction_date=datetime.date(2026, 1, 15), narration="Owner capital",
            created_by=cls.okafor,
        )
        post_bank_transaction(txn, actor_user=cls.okafor)

    @classmethod
    def staff(cls, tenant, *, hide=(), read_only=(), branch=None):
        name = f"bankacct-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@bank.test"), *KEYS,
            tenant=tenant, role_key=name, branch=branch,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, *cls.keys, read=True, write=True)
        if hide:
            set_field_access(role, *_k(*hide), read=False, write=False)
        if read_only:
            set_field_access(role, *_k(*read_only), read=True, write=False)
        return user

    def api(self, user):
        return TenantAPIClient(user=user)

    def listed(self, user, books=None, query=""):
        response = self.api(user).get(self.url("bank-accounts/", books) + query)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def row(self, user, bank=None, books=None):
        bank = bank or self.ikeja_bank
        return next(r for r in self.listed(user, books) if r["id"] == bank.pk)

    def detail(self, user, bank=None, books=None):
        bank = bank or self.ikeja_bank
        response = self.api(user).get(self.url(f"bank-accounts/{bank.pk}/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class BankAccountReadTests(_BankAccountFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.no_bank = cls.staff(cls.tenant, hide=("bank_name", "bank_code"))
        cls.no_setup = cls.staff(cls.tenant, hide=SETUP)
        cls.no_balance = cls.staff(cls.tenant, hide=("book_balance",))
        cls.no_statement = cls.staff(cls.tenant, hide=("statement_balance",))
        cls.no_progress = cls.staff(cls.tenant, hide=("reconciliation",))
        cls.no_activity = cls.staff(cls.tenant, hide=("recent_activity",))
        cls.no_number = cls.staff(cls.tenant, hide=("account_number",))
        cls.no_gateway = cls.staff(cls.tenant, hide=("gateway_subaccount",))
        cls.solo_open = cls.staff(cls.solo_tenant)
        cls.solo_no_bank = cls.staff(cls.solo_tenant, hide=("bank_name",))
        cls.default_balance_reader = cls.grant(
            cls.user_for(cls.tenant, "statement-default@bank.test"),
            "finance.bankaccount.view", tenant=cls.tenant,
            role_key="statement-default",
        )

    def test_statement_balance_starts_closed_without_a_field_grant(self):
        data = self.detail(self.default_balance_reader)
        self.assertNotIn("statement_balance", data["metrics"])
        self.assertIn("book_balance", data["metrics"])

    def test_hidden_bank_leaves_the_list_the_detail_and_the_provider_code(self):
        for shown in (self.row(self.no_bank), self.detail(self.no_bank)):
            for name in ("bank_name", "settlement_bank_code"):
                self.assertNotIn(name, shown)
            self.assertEqual(shown["name"], "Ikeja GTBank")
            self.assertIn("account_number", shown)
        full = self.detail(self.open)
        self.assertEqual(
            (full["bank_name"], full["account_number"], full["settlement_bank_code"]),
            ("Guaranty Trust Bank", "0123456789", "058"),
        )

    def test_hidden_setup_takes_every_alias_with_it(self):
        for shown in (self.row(self.no_setup), self.detail(self.no_setup)):
            for name in ("currency", "gl_account", "gl_account_name", "gl_account_id",
                         "is_active", "is_primary", "is_primary_collection"):
                self.assertNotIn(name, shown)
            self.assertIn("bank_name", shown)
        self.assertEqual(self.row(self.no_setup)["branch_id"], self.ikeja.pk)
        full = self.row(self.open)
        self.assertTrue(full["is_active"])

    def test_each_setup_field_has_its_own_switch(self):
        for name, shown in (
            ("currency", "currency"), ("ledger_account", "gl_account"),
            ("is_active", "is_active"),
            ("is_primary", "is_primary"), ("is_primary_collection", "is_primary_collection"),
        ):
            row = self.row(self.staff(self.tenant, hide=(name,)))
            with self.subTest(hidden=name):
                self.assertNotIn(shown, row)
                for other in {"currency", "gl_account", "is_active", "is_primary",
                              "is_primary_collection"} - {shown}:
                    self.assertIn(other, row)

    def test_the_account_number_keeps_its_own_switch(self):
        self.assertNotIn("account_number", self.row(self.no_number))
        self.assertIn("bank_name", self.row(self.no_number))

    def test_hidden_balance_leaves_every_figure_that_restates_it(self):
        row = self.row(self.no_balance)
        self.assertNotIn("book_balance", row)
        self.assertNotIn("book_balance_naira", row)
        data = self.detail(self.no_balance)
        self.assertNotIn("book_balance", data.get("metrics", {}))
        self.assertNotIn("unreconciled_diff", data.get("metrics", {}))
        for line in data["transactions"]:
            self.assertNotIn("running_balance", line)
        for run in data["reconciliations"]:
            self.assertNotIn("book_balance", run)
            self.assertNotIn("book_balance_naira", run)
            self.assertNotIn("difference", run)
        self.assertIn("statement_balance", data["metrics"])
        full = self.detail(self.open)
        self.assertIn("unreconciled_diff", full["metrics"])
        self.assertIn("running_balance", full["transactions"][0])

    def test_hidden_statement_balance_leaves_the_difference_and_the_run_figures(self):
        data = self.detail(self.no_statement)
        self.assertNotIn("statement_balance", data["metrics"])
        self.assertNotIn("unreconciled_diff", data["metrics"])
        for run in data["reconciliations"]:
            self.assertNotIn("statement_balance", run)
            self.assertNotIn("statement_balance_naira", run)
            self.assertNotIn("difference", run)
            self.assertIn("book_balance", run)

    def test_hidden_reconciliation_progress_leaves_the_count_and_the_last_run(self):
        row = self.row(self.no_progress)
        self.assertNotIn("unreconciled_count", row)
        self.assertNotIn("last_reconciled_at", row)
        self.assertNotIn("unreconciled_count", self.detail(self.no_progress)["metrics"])
        self.assertEqual(self.row(self.open)["unreconciled_count"], 1)

    def test_hidden_recent_activity_leaves_the_detail_list(self):
        self.assertNotIn("transactions", self.detail(self.no_activity))
        self.assertTrue(self.detail(self.open)["transactions"] is not None)

    def test_hidden_provider_route_leaves_the_subaccount_keys(self):
        row = self.row(self.no_gateway)
        self.assertNotIn("gateway_subaccount_code", row)
        self.assertNotIn("gateway_subaccount_provider", row)
        self.assertIn("settlement_bank_code", row)
        self.assertIn("gateway_subaccount_code", self.row(self.open))

    def test_a_one_branch_school_hides_the_same_fields(self):
        row = self.row(self.solo_no_bank, self.solo_bank, self.solo_books)
        self.assertNotIn("bank_name", row)
        self.assertIn("branch_id", row)
        full = self.row(self.solo_open, self.solo_bank, self.solo_books)
        self.assertEqual(full["bank_name"], "Zenith Bank")

    def test_a_filter_on_a_hidden_field_is_refused_and_answers_a_reader(self):
        refused = self.api(self.no_setup).get(self.url("bank-accounts/") + "&is_active=true")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_read_denied")
        answered = self.api(self.open).get(self.url("bank-accounts/") + "&is_active=true")
        self.assertEqual(answered.status_code, 200, answered.data)

    def test_the_map_lists_every_alias_of_a_hidden_field(self):
        payload = field_access_payload(self.no_setup, self.tenant)[BANK]
        for name in ("currency", "gl_account", "gl_account_name", "gl_account_id",
                     "is_active", "is_primary", "is_primary_collection"):
            self.assertIn(name, payload["hidden"])

    def test_the_audit_trail_of_a_split_shows_the_flags_only_to_a_reader_of_them(self):
        from vs_finance.audit import record
        from vs_finance.constants import FinanceAuditAction

        record(
            entity=self.books, action=FinanceAuditAction.ACCOUNT_UPDATED,
            actor_user=self.okafor, target=self.ikeja_bank, message="Bank account changed.",
            before={"is_active": True, "is_primary": True},
            after={"is_active": False, "is_primary": False},
        )
        auditor = self.staff(self.tenant, hide=("is_primary",))

        def snapshot(user):
            response = self.api(user).get(
                f"/v1/finance/audit-logs/?target_type=BankAccount&entity={self.books.code}")
            self.assertEqual(response.status_code, 200, response.data)
            return response.data["data"][0]["before"]

        self.assertEqual(snapshot(auditor), {"is_active": True})
        self.assertEqual(snapshot(self.open), {"is_active": True, "is_primary": True})


class BankAccountWriteTests(_BankAccountFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.ro_bank = cls.staff(cls.tenant, read_only=("bank_name", "account_number"))
        cls.ro_setup = cls.staff(cls.tenant, read_only=SETUP)
        cls.no_bank = cls.staff(cls.tenant, hide=("bank_name",))
        cls.solo_ro = cls.staff(cls.solo_tenant, read_only=("bank_name", "account_number"))

    def patch(self, user, body, bank=None, books=None):
        bank = bank or self.ikeja_bank
        return self.api(user).patch(
            self.url(f"bank-accounts/{bank.pk}/", books), body, format="json")

    def fresh(self, bank=None):
        bank = bank or self.ikeja_bank
        bank.refresh_from_db()
        return bank

    def test_a_detail_lists_the_read_only_fields(self):
        data = self.detail(self.ro_bank)
        self.assertEqual(
            sorted(data["_read_only_fields"]),
            sorted(["bank_name", "account_number", "book_balance", "book_balance_naira",
                    "unreconciled_count", "last_reconciled_at", "transactions",
                    "gateway_subaccount_code", "gateway_subaccount_provider"]),
        )
        self.assertNotIn("_read_only_fields", self.row(self.ro_bank))

    def test_changing_a_read_only_bank_detail_is_refused_and_nothing_is_saved(self):
        for name, value in (("bank_name", "Access Bank"), ("account_number", "9999999999")):
            response = self.patch(self.ro_bank, {name: value})
            with self.subTest(field=name):
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")
                self.assertIn(name, response.data["error"]["detail"])
        saved = self.fresh()
        self.assertEqual((saved.bank_name, saved.account_number),
                         ("Guaranty Trust Bank", "0123456789"))

    def test_a_form_that_sends_the_stored_values_back_is_not_refused(self):
        response = self.patch(self.ro_bank, {
            "name": "Ikeja GTBank Operations", "bank_name": "Guaranty Trust Bank",
            "account_number": "0123456789",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fresh().name, "Ikeja GTBank Operations")

    def test_a_hidden_bank_is_refused_even_when_the_value_matches(self):
        response = self.patch(self.no_bank, {"bank_name": "Guaranty Trust Bank"})
        self.assertEqual(response.status_code, 403, response.data)

    def test_read_only_setup_is_refused_when_it_changes(self):
        for body in ({"is_active": False}, {"is_primary": True}, {"is_primary_collection": True},
                     {"currency": "NGN"}):
            response = self.patch(self.ro_setup, body)
            with self.subTest(body=body):
                self.assertEqual(response.status_code, 403, response.data)
        saved = self.fresh()
        self.assertTrue(saved.is_active)
        echo = self.patch(self.ro_setup, {"is_active": True, "is_primary": saved.is_primary})
        self.assertEqual(echo.status_code, 200, echo.data)

    def test_a_writable_role_changes_the_bank_and_the_response_echoes_it(self):
        response = self.patch(self.open, {"bank_name": "Access Bank"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["bank_name"], "Access Bank")
        self.assertEqual(self.fresh().bank_name, "Access Bank")

    def test_the_response_to_a_write_hides_what_the_caller_may_not_read(self):
        user = self.staff(self.tenant, hide=("account_number",))
        response = self.patch(user, {"name": "Ikeja GTBank Main"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("account_number", response.data["data"])
        self.assertIn("bank_name", response.data["data"])

    def test_a_one_branch_school_refuses_the_same_write(self):
        response = self.patch(
            self.solo_ro, {"account_number": "1"}, self.solo_bank, self.solo_books)
        self.assertEqual(response.status_code, 403, response.data)

    # -- create ----------------------------------------------------------------- #

    def body(self, name="New Savings", **extra):
        return {
            "name": name, "gl_account": "1100", "branch": self.ikeja.pk, **extra,
        }

    def create(self, user, **extra):
        return self.api(user).post(self.url("bank-accounts/"), self.body(**extra), format="json")

    def test_a_create_that_sets_a_closed_field_is_refused_and_saves_nothing(self):
        before = BankAccount.objects.filter(entity=self.books).count()
        cases = (
            (self.ro_bank, {"bank_name": "Access Bank"}),
            (self.ro_bank, {"account_number": "1234567890"}),
            (self.ro_setup, {"is_primary": True}),
            (self.ro_setup, {"currency": "NGN"}),
        )
        for index, (user, extra) in enumerate(cases):
            response = self.create(user, name=f"Refused {index}", **extra)
            with self.subTest(extra=extra):
                self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(BankAccount.objects.filter(entity=self.books).count(), before)

    def test_a_read_only_role_can_still_open_an_account_on_a_ledger_account_and_branch(self):
        response = self.create(self.ro_setup, name="Opened By Clerk")
        self.assertEqual(response.status_code, 201, response.data)

    def test_defaults_left_alone_are_not_a_write(self):
        response = self.create(
            self.ro_setup, name="Defaults", is_active=True, is_primary=False,
            is_primary_collection=False)
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_open_role_creates_with_every_field(self):
        response = self.create(
            self.open, name="Full", bank_name="Access Bank", account_number="1234567890")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["bank_name"], "Access Bank")

    def test_create_and_update_record_account_number_in_the_audit_snapshot(self):
        response = self.create(self.open, name="Audit Bank", account_number="1234567890")
        self.assertEqual(response.status_code, 201, response.data)
        bank = BankAccount.objects.get(name="Audit Bank", entity=self.books)
        created = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.BANK_ACCOUNT_CREATED, target_id=str(bank.pk))
        # The trail keeps the last four, never the whole number (core.encryption.withhold).
        self.assertEqual(created.after["account_number"], "****7890")
        response = self.patch(self.open, {"account_number": "9999999999"}, bank=bank)
        self.assertEqual(response.status_code, 200, response.data)
        updated = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.BANK_ACCOUNT_UPDATED, target_id=str(bank.pk))
        self.assertEqual(updated.before["account_number"], "****7890")
        self.assertEqual(updated.after["account_number"], "****9999")

    def test_primary_account_demotion_has_its_own_audit_row(self):
        BankAccount.objects.filter(pk=self.ikeja_bank.pk).update(is_primary=True)
        response = self.create(self.open, name="New Primary", is_primary=True)
        self.assertEqual(response.status_code, 201, response.data)
        demotion = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.BANK_ACCOUNT_UPDATED,
            target_id=str(self.ikeja_bank.pk),
        )
        self.assertTrue(demotion.before["is_primary"])
        self.assertFalse(demotion.after["is_primary"])

    def test_account_number_in_new_audit_row_follows_read_switch(self):
        self.create(self.open, name="Hidden Number", account_number="1234567890")
        reader = self.staff(self.tenant, hide=("account_number",))
        response = self.api(reader).get(
            f"/v1/finance/audit-logs/?entity={self.books.code}&action=BANK_ACCOUNT_CREATED")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("account_number", response.data["data"][0]["after"])

    # -- tenants ---------------------------------------------------------------- #

    def test_one_schools_closed_role_does_not_close_another_schools_accounts(self):
        rival = self.staff(self.rival_tenant)
        self.assertEqual(self.api(rival).get(self.url("bank-accounts/", self.rival_books))
                         .status_code, 200)
        closed = self.api(self.no_bank).get(
            f"/v1/finance/bank-accounts/{self.ikeja_bank.pk}/?entity={self.rival_books.code}")
        self.assertIn(closed.status_code, (403, 404))


class BankAccountExportTests(_BankAccountFieldAccess):
    """Bank accounts reach no export dataset, so no column or filter carries their fields."""

    def test_no_dataset_exports_a_bank_account_field(self):
        from vs_exports.catalogue import all_datasets

        for dataset in all_datasets():
            for column in dataset.fields:
                with self.subTest(dataset=dataset.key, column=column.id):
                    keys = (column.access,) if isinstance(column.access, str) else column.access
                    self.assertFalse(any(key.startswith(f"{BANK}.") for key in keys))


class DocumentSettingsBankTests(_BankAccountFieldAccess):
    """The settings screen that chooses the fee collection account is another way to write and read it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.settings_keys = ("finance.settings.view", "finance.settings.update")
        cls.open = cls.settings_user(hide=())
        cls.no_bank = cls.settings_user(hide=("bank_name", "currency"))
        cls.ro_flag = cls.settings_user(read_only=("is_primary_collection",))

    @classmethod
    def settings_user(cls, *, hide=(), read_only=()):
        name = f"banksettings-{next(_seq)}"
        user = cls.grant(
            cls.user_for(cls.tenant, f"{name}@bank.test"), *KEYS, *cls.settings_keys,
            tenant=cls.tenant, role_key=name,
        )
        role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key=name)
        set_field_access(role, *cls.keys, read=True, write=True)
        if hide:
            set_field_access(role, *_k(*hide), read=False, write=False)
        if read_only:
            set_field_access(role, *_k(*read_only), read=True, write=False)
        return user

    def screen(self, user):
        response = self.api(user).get(self.url("settings/documents/"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["settings"]

    def test_the_bank_summaries_drop_what_the_caller_may_not_read(self):
        full = self.screen(self.open)["bank_account_options"][0]
        self.assertEqual(full["bank_name"], "Guaranty Trust Bank")
        hidden = self.screen(self.no_bank)
        for summary in (*hidden["bank_account_options"], *hidden["primary_collection_bank_accounts"]):
            for name in ("bank_name", "currency"):
                self.assertNotIn(name, summary)
            self.assertIn("name", summary)
            self.assertIn("branch_name", summary)

    def test_the_history_of_the_collection_account_drops_the_bank_and_currency_too(self):
        changed = self.api(self.open).patch(
            self.url("settings/documents/"),
            {"primary_collection_bank_account": self.lekki_bank.pk}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)

        def snapshots(user):
            data = self.api(user).get(self.url("settings/documents/")).data["data"]
            entry = next(row for row in data["history"]
                         if "primary_collection_bank_account" in (row["after"] or {}))
            return entry["before"], entry["after"]

        _, after = snapshots(self.open)
        self.assertIn("bank_name", after["primary_collection_bank_account"])
        self.assertIn("currency", after["primary_collection_bank_account"])
        for snapshot in snapshots(self.no_bank):
            summary = snapshot.get("primary_collection_bank_account")
            if summary is None:
                continue
            for name in ("bank_name", "currency"):
                self.assertNotIn(name, summary)
            self.assertIn("name", summary)

    def test_choosing_another_collection_account_is_a_write_of_that_flag(self):
        response = self.api(self.ro_flag).patch(
            self.url("settings/documents/"),
            {"primary_collection_bank_account": self.lekki_bank.pk}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("is_primary_collection", response.data["error"]["detail"])
        self.assertFalse(BankAccount.objects.get(pk=self.lekki_bank.pk).is_primary_collection)

    def test_leaving_the_collection_account_as_it_is_is_not_a_write(self):
        response = self.api(self.ro_flag).patch(
            self.url("settings/documents/"), {"default_invoice_due_days": 10}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        current = BankAccount.objects.filter(entity=self.books, is_primary_collection=True).first()
        if current is not None:
            same = self.api(self.ro_flag).patch(
                self.url("settings/documents/"),
                {"primary_collection_bank_account": current.pk}, format="json")
            self.assertEqual(same.status_code, 200, same.data)

    def test_an_open_role_chooses_the_account(self):
        response = self.api(self.open).patch(
            self.url("settings/documents/"),
            {"primary_collection_bank_account": self.lekki_bank.pk}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(BankAccount.objects.get(pk=self.lekki_bank.pk).is_primary_collection)
