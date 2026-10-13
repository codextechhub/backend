"""Field Access on bank transactions, transfers between own accounts and the branch split.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch; Rival Group is a
third school. Each class builds one role per access state in ``setUpTestData``
(a role that cannot read a field, one that can read and not write it) and drives
the real endpoints as those users, so a rule is proved on the route that serves
it and on every other route that carries the same value.
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient
from vs_finance.banking import post_bank_transaction, post_bank_transfer
from vs_finance.models import (
    Account,
    BankAccount,
    BankStatement,
    BankTransaction,
    BankTransfer,
    JournalEntry,
    JournalLine,
)
from vs_finance.constants import DocumentStatus, JournalSource
from vs_finance.posting import post_journal, resolve_period
from vs_finance.tests_bank_document_rework import KEYS, _ReworkFixture
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_branch_scope import _FinanceBranchFixture

TXN = "finance.banktransaction"
TRF = "finance.banktransfer"
BANK = "finance.bankaccount"

ALL_KEYS = KEYS + (
    "finance.banktransaction.reverse", "finance.banktransfer.reverse",
)
_seq = itertools.count(1)


def _role_of(user, tenant):
    """The role a user built by :func:`_staff` holds."""
    return TenantRoleTemplate.objects.get(tenant=tenant, key=user.email.split("@")[0])


class _BankFieldAccess(_ReworkFixture):
    """The rework books, plus helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.txn_keys = install_declared_fields(TXN)
        cls.transfer_keys = install_declared_fields(TRF)
        cls.okafor_posted_txn = cls.posted_transaction()
        cls.okafor_posted_transfer = cls.posted_transfer()

    @classmethod
    def posted_transaction(cls):
        txn = BankTransaction.objects.create(
            entity=cls.books, branch=cls.ikeja, bank_account=cls.ikeja_bank, direction="IN",
            amount=7_000_000, counter_account=cls.acc("3100"),
            transaction_date=datetime.date(2026, 1, 15), narration="Owner capital, posted",
            reference="CAP-7", created_by=cls.okafor,
        )
        post_bank_transaction(txn, actor_user=cls.okafor)
        txn.refresh_from_db()
        return txn

    @classmethod
    def posted_transfer(cls):
        transfer = BankTransfer.objects.create(
            entity=cls.books, branch=cls.ikeja, from_account=cls.ikeja_zenith,
            to_account=cls.ikeja_bank, amount=1_000_000,
            transfer_date=datetime.date(2026, 1, 15), narration="Fund GTBank, posted",
            reference="TR-7", created_by=cls.okafor,
        )
        post_bank_transfer(transfer, actor_user=cls.okafor)
        transfer.refresh_from_db()
        return transfer

    @classmethod
    def staff(cls, tenant, resource_keys, *, hide=(), read_only=(), branch=None):
        """A user holding every bank document permission, with the named field states.

        ``hide`` closes Read and Write, and ``read_only`` leaves Read and closes
        Write. A write without a read is not a state a role can hold.
        """
        name = f"bankfa-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@bank.test"), *ALL_KEYS,
            tenant=tenant, role_key=name, branch=branch,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, *resource_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        return user

    def api(self, user):
        return TenantAPIClient(user=user)

    def code(self, response):
        return response.data["error"]["code"]

    def flip(self, user, tenant, keys, *, read, write):
        """Change one field state of a user's role after the document exists."""
        set_field_access(_role_of(user, tenant), *keys, read=read, write=write)


def _k(resource, *names):
    return [f"{resource}.{name}" for name in names]


# --------------------------------------------------------------------------- #
# Bank transactions                                                           #
# --------------------------------------------------------------------------- #

class BankTransactionFieldAccessTests(_BankFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = cls.txn_keys
        cls.open = cls.staff(cls.tenant, keys)
        cls.no_values = cls.staff(cls.tenant, keys, hide=_k(
            TXN, "amount", "narration", "reference", "direction", "transaction_date"))
        cls.no_accounts = cls.staff(
            cls.tenant, keys, hide=_k(TXN, "bank_account", "counter_account"))
        cls.no_identity = cls.staff(cls.tenant, keys, hide=_k(TXN, "status", "journal"))
        cls.ro_values = cls.staff(cls.tenant, keys, read_only=_k(
            TXN, "amount", "narration", "bank_account", "reference"))
        cls.ro_reference = cls.staff(cls.tenant, keys, read_only=_k(TXN, "reference"))
        cls.solo_open = cls.staff(cls.solo_tenant, keys)
        cls.solo_txn = BankTransaction.objects.create(
            entity=cls.solo_books, branch=cls.solo_main, bank_account=cls.solo_bank,
            direction="IN", amount=300_000, counter_account=Account.objects.get(
                entity=cls.solo_books, code="3100"),
            transaction_date=datetime.date(2026, 1, 15), narration="Solo capital",
        )

    def detail(self, user, txn=None, books=None):
        txn = txn or self.okafor_posted_txn
        return self.api(user).get(self.url(f"bank-transactions/{txn.pk}/", books))

    def rejected(self, creator):
        pk = self.capital(creator)
        self.reject(BankTransaction, pk)
        return pk

    def body(self, **extra):
        return {
            "bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": 1_500_000,
            "counter_account": "3100", "transaction_date": "2026-01-15",
            "narration": "Owner capital", **extra,
        }

    # -- reads ---------------------------------------------------------------- #

    def test_hidden_values_leave_the_list_and_the_detail(self):
        listed = self.api(self.no_values).get(self.url("bank-transactions/")).data["data"]
        self.assertTrue(listed)
        detail = self.detail(self.no_values).data["data"]
        for row in (*listed, detail):
            for name in ("amount", "narration", "reference", "direction", "transaction_date"):
                self.assertNotIn(name, row)
            self.assertIn("document_number", row)
            self.assertIn("bank_account_name", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["amount"], full["narration"], full["reference"], full["direction"]),
            (7_000_000, "Owner capital, posted", "CAP-7", "IN"),
        )

    def test_hidden_accounts_take_their_ids_codes_and_names_with_them(self):
        rows = [
            *self.api(self.no_accounts).get(self.url("bank-transactions/")).data["data"],
            self.detail(self.no_accounts).data["data"],
        ]
        for row in rows:
            for name in ("bank_account_id", "bank_account_name", "counter_account_id",
                         "counter_account_code", "counter_account_name"):
                self.assertNotIn(name, row)
            self.assertIn("amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["bank_account_name"], full["counter_account_code"]),
            ("Ikeja GTBank", "3100"),
        )

    def test_hidden_identity_leaves_status_approval_and_journal_and_keeps_number_and_branch(self):
        rows = [
            *self.api(self.no_identity).get(self.url("bank-transactions/")).data["data"],
            self.detail(self.no_identity).data["data"],
        ]
        for row in rows:
            for name in ("status", "approval_state", "approval_returned", "journal_id"):
                self.assertNotIn(name, row)
            for name in ("document_number", "branch_id", "branch_name"):
                self.assertIn(name, row)
            self.assertIn("amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(full["status"], DocumentStatus.POSTED)
        self.assertEqual(full["branch_name"], "Ikeja Branch")
        self.assertEqual(full["journal_id"], self.okafor_posted_txn.journal_id)

    def test_read_only_names_are_listed_on_a_detail_and_never_on_a_list_row(self):
        detail = self.detail(self.ro_values).data["data"]
        self.assertEqual(
            sorted(detail["_read_only_fields"]),
            sorted([
                "amount", "narration", "reference", "bank_account_id", "bank_account_name",
                "bank_account", "status", "approval_state",
                "approval_returned", "journal_id",
            ]),
        )
        self.assertEqual(sorted(self.detail(self.open).data["data"]["_read_only_fields"]), sorted([
            "status", "approval_state", "approval_returned", "journal_id",
        ]))
        for row in self.api(self.ro_values).get(self.url("bank-transactions/")).data["data"]:
            self.assertNotIn("_read_only_fields", row)

    def test_the_body_keys_a_form_sends_are_listed_when_their_field_is_read_only(self):
        listed = self.detail(self.ro_values).data["data"]["_read_only_fields"]
        self.assertIn("bank_account", listed)
        self.assertNotIn("counter_account", listed)
        self.assertNotIn("bank_account", self.detail(self.open).data["data"]["_read_only_fields"])

    def test_the_me_map_lists_the_aliases_a_form_sends(self):
        payload = field_access_payload(self.ro_values, self.tenant)[TXN]
        self.assertIn("bank_account", payload["read_only"])
        self.assertIn("amount", payload["read_only"])
        self.assertNotIn("counter_account", payload["read_only"])

    def test_a_filter_on_a_hidden_field_is_refused_not_answered(self):
        for user, query, label in (
            (self.no_accounts, f"bank_account={self.ikeja_bank.pk}", "Bank account"),
            (self.no_identity, "status=POSTED", "Status"),
            (self.no_identity, "approval=returned", "Status"),
        ):
            with self.subTest(query=query):
                response = self.api(user).get(self.url("bank-transactions/") + f"&{query}")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])
        open_filtered = self.api(self.open).get(
            self.url("bank-transactions/") + f"&bank_account={self.ikeja_bank.pk}&status=POSTED")
        self.assertEqual(open_filtered.status_code, 200, open_filtered.data)
        self.assertTrue(open_filtered.data["data"])

    # -- writes --------------------------------------------------------------- #

    def test_a_create_that_sets_a_read_only_field_is_refused_and_saves_nothing(self):
        before = BankTransaction.objects.count()
        for extra, label in (
            ({"amount": 2_000_000}, "Amount"),
            ({"narration": "Gift"}, "Narration"),
            ({"bank_account": self.ikeja_bank.pk}, "Bank account"),
        ):
            with self.subTest(extra=extra):
                response = self.api(self.ro_values).post(
                    self.url("bank-transactions/"), self.body(**extra), format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_write_denied")
                self.assertIn(label, response.data["message"])
        self.assertEqual(BankTransaction.objects.count(), before)

    def test_an_open_role_creates_and_the_message_names_the_number(self):
        response = self.api(self.open).post(
            self.url("bank-transactions/"), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        number = BankTransaction.objects.latest("id").document_number
        self.assertIn(number, response.data["message"])
        self.assertEqual(response.data["data"]["amount"], 1_500_000)

    def test_a_hidden_status_is_left_out_of_the_payload_and_the_number_stays_in_the_sentence(self):
        response = self.api(self.no_identity).post(
            self.url("bank-transactions/"), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        number = BankTransaction.objects.latest("id").document_number
        self.assertEqual(
            response.data["message"],
            f"Bank transaction {number} is waiting for approval. "
            "It reaches the books once it is approved.",
        )
        self.assertEqual(response.data["data"]["document_number"], number)
        self.assertNotIn("approval_state", response.data["data"])

    def test_a_form_that_leaves_a_read_only_input_empty_is_not_refused(self):
        empty = self.api(self.ro_reference).post(
            self.url("bank-transactions/"), self.body(reference=""), format="json")
        self.assertEqual(empty.status_code, 201, empty.data)
        filled = self.api(self.ro_reference).post(
            self.url("bank-transactions/"), self.body(reference="REF-1"), format="json")
        self.assertEqual(filled.status_code, 403, filled.data)
        self.assertEqual(list(filled.data["error"]["detail"]), ["reference"])

    def test_every_read_only_field_is_named_in_one_refusal(self):
        response = self.api(self.ro_values).post(
            self.url("bank-transactions/"), self.body(narration="Gift"), format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            sorted(response.data["error"]["detail"]),
            ["amount", "bank_account", "narration"],
        )

    def test_a_correction_is_judged_by_what_it_changes(self):
        pk = self.rejected(self.open)
        self.flip(self.open, self.tenant, _k(TXN, "amount", "narration"), read=True, write=False)
        path = self.url(f"bank-transactions/{pk}/")

        changed = self.api(self.open).patch(path, {"amount": 4_000_000}, format="json")
        self.assertEqual(changed.status_code, 403, changed.data)
        self.assertIn("Amount", changed.data["message"])
        self.assertEqual(BankTransaction.objects.get(pk=pk).amount, 5_000_000)

        echoed = self.api(self.open).patch(
            path, {"amount": 5_000_000, "narration": "Owner capital", "reference": "REF-9"},
            format="json")
        self.assertEqual(echoed.status_code, 200, echoed.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).reference, "REF-9")

        self.flip(self.open, self.tenant, _k(TXN, "amount", "narration"), read=True, write=True)
        written = self.api(self.open).patch(path, {"amount": 4_000_000}, format="json")
        self.assertEqual(written.status_code, 200, written.data)
        self.assertEqual(BankTransaction.objects.get(pk=pk).amount, 4_000_000)

    def test_a_correction_that_moves_the_bank_account_is_refused_by_its_body_name(self):
        user = self.staff(self.tenant, self.txn_keys)
        pk = self.rejected(user)
        self.flip(user, self.tenant, _k(TXN, "bank_account"), read=True, write=False)

        moved = self.api(user).patch(
            self.url(f"bank-transactions/{pk}/"), {"bank_account": self.lekki_bank.pk},
            format="json")

        self.assertEqual(moved.status_code, 403, moved.data)
        self.assertIn("Bank account", moved.data["message"])
        self.assertEqual(BankTransaction.objects.get(pk=pk).bank_account_id, self.ikeja_bank.pk)

    def test_the_submit_cancel_and_void_responses_are_filtered_and_name_the_number(self):
        sender = self.staff(self.tenant, self.txn_keys)
        pk = self.rejected(sender)
        self.flip(sender, self.tenant, _k(TXN, "status", "journal"), read=False, write=False)
        client = self.api(sender)
        number = BankTransaction.objects.get(pk=pk).document_number

        sent = client.post(self.url(f"bank-transactions/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertNotIn("status", sent.data["data"])
        self.assertIn("approval", sent.data["data"])
        self.assertEqual(
            sent.data["message"],
            f"Bank transaction {number} is waiting for approval. "
            "It reaches the books once it is approved.",
        )

        self.reject(BankTransaction, pk)
        cancelled = client.post(self.url(f"bank-transactions/{pk}/cancel/"), {}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["message"], f"Bank transaction {number} cancelled.")
        self.assertNotIn("status", cancelled.data["data"])

        posted_number = self.okafor_posted_txn.document_number
        voided = client.post(
            self.url(f"bank-transactions/{self.okafor_posted_txn.pk}/void/"), {}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["message"], f"Bank transaction {posted_number} voided.")
        for name in ("status", "journal_id"):
            self.assertNotIn(name, voided.data["data"])
        self.assertEqual(voided.data["data"]["document_number"], posted_number)
        self.assertEqual(
            BankTransaction.objects.get(pk=self.okafor_posted_txn.pk).status,
            DocumentStatus.REVERSED,
        )

    # -- shapes of school ----------------------------------------------------- #

    def test_a_one_branch_school_and_a_multi_branch_school_both_show_the_branch(self):
        shown = self.api(self.solo_open).get(
            self.url("bank-transactions/", self.solo_books)).data["data"]
        self.assertEqual(shown[0]["branch_name"], "Main Branch")
        self.assertEqual(self.detail(self.open).data["data"]["branch_name"], "Ikeja Branch")
        self.assertEqual(self.detail(self.no_identity).data["data"]["branch_name"], "Ikeja Branch")

    def test_one_schools_switches_do_not_reach_another_schools_roles(self):
        # Corona's hidden-amount role does not change what Single Site's role reads.
        self.assertNotIn("amount", self.api(self.no_values).get(
            self.url("bank-transactions/")).data["data"][0])
        solo = self.api(self.solo_open).get(self.url("bank-transactions/", self.solo_books))
        self.assertEqual(solo.data["data"][0]["amount"], 300_000)
        # Another school's bursar reaches none of Corona's documents, with or without switches.
        rival = self.api(self.rival).get(
            self.url(f"bank-transactions/{self.okafor_posted_txn.pk}/", self.books))
        self.assertIn(rival.status_code, (403, 404))


# --------------------------------------------------------------------------- #
# Transfers between own accounts                                              #
# --------------------------------------------------------------------------- #

class BankTransferFieldAccessTests(_BankFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = cls.transfer_keys
        cls.open = cls.staff(cls.tenant, keys)
        cls.no_values = cls.staff(cls.tenant, keys, hide=_k(
            TRF, "amount", "narration", "reference", "transfer_date"))
        cls.no_sending = cls.staff(cls.tenant, keys, hide=_k(TRF, "from_account"))
        cls.no_receiving = cls.staff(cls.tenant, keys, hide=_k(TRF, "to_account"))
        cls.no_identity = cls.staff(cls.tenant, keys, hide=_k(TRF, "status", "journal"))
        cls.ro_values = cls.staff(cls.tenant, keys, read_only=_k(
            TRF, "amount", "from_account", "to_account"))
        cls.solo_open = cls.staff(cls.solo_tenant, keys)
        solo_ledger = Account.objects.create(
            entity=cls.solo_books, code="1153", name="Solo savings",
            account_type=cls.acc("1100").account_type, is_postable=True,
        )
        cls.solo_savings = BankAccount.objects.create(
            entity=cls.solo_books, name="Solo savings", branch=cls.solo_main,
            gl_account=solo_ledger,
        )
        cls.solo_transfer = BankTransfer.objects.create(
            entity=cls.solo_books, branch=cls.solo_main, from_account=cls.solo_savings,
            to_account=cls.solo_bank, amount=250_000,
            transfer_date=datetime.date(2026, 1, 15), narration="Solo top-up",
        )

    def detail(self, user, transfer=None, books=None):
        transfer = transfer or self.okafor_posted_transfer
        return self.api(user).get(self.url(f"bank-transfers/{transfer.pk}/", books))

    def body(self, **extra):
        return {
            "from_account": self.ikeja_zenith.pk, "to_account": self.ikeja_bank.pk,
            "amount": 2_000_000, "transfer_date": "2026-01-15", "narration": "Fund GTBank",
            **extra,
        }

    def rejected(self, creator):
        pk = self.transfer(creator)
        self.reject(BankTransfer, pk)
        return pk

    def test_hidden_values_leave_the_list_and_the_detail(self):
        rows = [
            *self.api(self.no_values).get(self.url("bank-transfers/")).data["data"],
            self.detail(self.no_values).data["data"],
        ]
        for row in rows:
            for name in ("amount", "narration", "reference", "transfer_date"):
                self.assertNotIn(name, row)
            self.assertIn("from_account_name", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual((full["amount"], full["reference"]), (1_000_000, "TR-7"))

    def test_each_account_is_hidden_on_its_own(self):
        sending = self.detail(self.no_sending).data["data"]
        self.assertNotIn("from_account_id", sending)
        self.assertNotIn("from_account_name", sending)
        self.assertEqual(sending["to_account_name"], "Ikeja GTBank")
        receiving = self.detail(self.no_receiving).data["data"]
        self.assertNotIn("to_account_id", receiving)
        self.assertNotIn("to_account_name", receiving)
        self.assertEqual(receiving["from_account_name"], "Ikeja Zenith")

    def test_hidden_identity_leaves_status_approval_and_journal_and_keeps_number_and_branch(self):
        for row in (
            *self.api(self.no_identity).get(self.url("bank-transfers/")).data["data"],
            self.detail(self.no_identity).data["data"],
        ):
            for name in ("status", "approval_state", "approval_returned", "journal_id"):
                self.assertNotIn(name, row)
            for name in ("document_number", "branch_id", "branch_name"):
                self.assertIn(name, row)

    def test_read_only_names_are_listed_on_a_detail(self):
        detail = self.detail(self.ro_values).data["data"]
        for name in ("amount", "from_account_id", "from_account_name", "to_account_id",
                     "to_account_name"):
            self.assertIn(name, detail["_read_only_fields"])
        self.assertNotIn("narration", detail["_read_only_fields"])

    def test_the_body_keys_a_form_sends_are_listed_when_their_field_is_read_only(self):
        listed = self.detail(self.ro_values).data["data"]["_read_only_fields"]
        self.assertIn("from_account", listed)
        self.assertIn("to_account", listed)
        open_listed = self.detail(self.open).data["data"]["_read_only_fields"]
        self.assertNotIn("from_account", open_listed)
        self.assertNotIn("to_account", open_listed)

    def test_a_filter_on_either_hidden_account_is_refused(self):
        for user in (self.no_sending, self.no_receiving):
            response = self.api(user).get(
                self.url("bank-transfers/") + f"&bank_account={self.ikeja_bank.pk}")
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(self.code(response), "field_read_denied")
        status = self.api(self.no_identity).get(self.url("bank-transfers/") + "&status=POSTED")
        self.assertEqual(status.status_code, 403, status.data)
        self.assertEqual(self.api(self.open).get(
            self.url("bank-transfers/") + f"&bank_account={self.ikeja_bank.pk}").status_code, 200)

    def test_a_create_that_sets_a_read_only_field_is_refused_and_saves_nothing(self):
        before = BankTransfer.objects.count()
        for extra, label in (
            ({"amount": 3_000_000}, "Amount"),
            ({"from_account": self.ikeja_zenith.pk}, "Sending account"),
            ({"to_account": self.ikeja_bank.pk}, "Receiving account"),
        ):
            with self.subTest(extra=extra):
                response = self.api(self.ro_values).post(
                    self.url("bank-transfers/"), self.body(**extra), format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_write_denied")
                self.assertIn(label, response.data["message"])
        self.assertEqual(BankTransfer.objects.count(), before)

    def test_a_create_names_the_number_whatever_the_status_switch_says(self):
        named = self.api(self.open).post(self.url("bank-transfers/"), self.body(), format="json")
        self.assertEqual(named.status_code, 201, named.data)
        number = BankTransfer.objects.latest("id").document_number
        self.assertIn(number, named.data["message"])
        plain = self.api(self.no_identity).post(
            self.url("bank-transfers/"), self.body(), format="json")
        self.assertEqual(plain.status_code, 201, plain.data)
        number = BankTransfer.objects.latest("id").document_number
        self.assertEqual(
            plain.data["message"],
            f"Transfer {number} is waiting for approval. It reaches the books once it is approved.",
        )
        self.assertEqual(plain.data["data"]["document_number"], number)
        self.assertNotIn("status", plain.data["data"])

    def test_a_correction_is_judged_by_what_it_changes(self):
        pk = self.rejected(self.open)
        self.flip(self.open, self.tenant, _k(TRF, "amount", "to_account"), read=True, write=False)
        path = self.url(f"bank-transfers/{pk}/")

        changed = self.api(self.open).patch(path, {"amount": 1_500_000}, format="json")
        self.assertEqual(changed.status_code, 403, changed.data)
        self.assertEqual(BankTransfer.objects.get(pk=pk).amount, 2_000_000)

        moved = self.api(self.open).patch(path, {"to_account": self.ikeja_zenith.pk}, format="json")
        self.assertEqual(moved.status_code, 403, moved.data)
        self.assertIn("Receiving account", moved.data["message"])
        self.assertEqual(BankTransfer.objects.get(pk=pk).to_account_id, self.ikeja_bank.pk)

        echoed = self.api(self.open).patch(
            path, {"amount": 2_000_000, "to_account": self.ikeja_bank.pk, "reference": "Z-1"},
            format="json")
        self.assertEqual(echoed.status_code, 200, echoed.data)
        self.assertEqual(BankTransfer.objects.get(pk=pk).reference, "Z-1")

        self.flip(self.open, self.tenant, _k(TRF, "amount"), read=True, write=True)
        written = self.api(self.open).patch(path, {"amount": 1_500_000}, format="json")
        self.assertEqual(written.status_code, 200, written.data)
        self.assertEqual(BankTransfer.objects.get(pk=pk).amount, 1_500_000)

    def test_the_cancel_and_void_responses_are_filtered_and_name_the_number(self):
        sender = self.staff(self.tenant, self.transfer_keys)
        pk = self.rejected(sender)
        self.flip(sender, self.tenant, _k(TRF, "status", "journal"), read=False, write=False)
        client = self.api(sender)
        number = BankTransfer.objects.get(pk=pk).document_number

        cancelled = client.post(self.url(f"bank-transfers/{pk}/cancel/"), {}, format="json")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["message"], f"Transfer {number} cancelled.")
        self.assertNotIn("status", cancelled.data["data"])

        posted_number = self.okafor_posted_transfer.document_number
        voided = client.post(
            self.url(f"bank-transfers/{self.okafor_posted_transfer.pk}/void/"), {}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["message"], f"Transfer {posted_number} voided.")
        self.assertEqual(voided.data["data"]["document_number"], posted_number)

    def test_the_submit_response_is_filtered(self):
        sender = self.staff(self.tenant, self.transfer_keys)
        pk = self.rejected(sender)
        self.flip(sender, self.tenant, _k(TRF, "status"), read=False, write=False)
        sent = self.api(sender).post(
            self.url(f"bank-transfers/{pk}/submit/"), {}, format="json")
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertNotIn("status", sent.data["data"])
        self.assertIn("approval", sent.data["data"])

    def test_a_one_branch_school_and_a_multi_branch_school_both_show_the_branch(self):
        shown = self.api(self.solo_open).get(
            self.url("bank-transfers/", self.solo_books)).data["data"]
        self.assertEqual(shown[0]["branch_name"], "Main Branch")
        self.assertEqual(self.detail(self.open).data["data"]["branch_name"], "Ikeja Branch")
        self.assertEqual(self.detail(self.no_identity).data["data"]["branch_name"], "Ikeja Branch")

    def test_another_schools_bursar_reaches_none_of_it(self):
        response = self.detail(self.rival, books=self.books)
        self.assertIn(response.status_code, (403, 404))


# --------------------------------------------------------------------------- #
# The branch split of a shared bank account                                   #
# --------------------------------------------------------------------------- #

JAN_10 = datetime.date(2026, 1, 10)
JAN_15 = datetime.date(2026, 1, 15)
SPLIT_KEYS = ("finance.bankaccount.view", "finance.bankaccount.update")


class BankSplitFieldAccessTests(_FinanceBranchFixture):
    """The cutover of a shared account into one account per branch.

    Bright Star's old GTBank record is shared by Ikeja and Lekki. The bursar
    previews the split and posts the agreed shares; a school may close any field
    of either for a role.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(BANK, "finance.interbranch")
        cls.legacy_ledger = Account.objects.create(
            entity=cls.books, parent=Account.objects.get(entity=cls.books, code="1100"),
            code="1150", name="Shared GTBank ledger", account_type="ASSET",
            normal_balance="DEBIT", is_postable=True,
        )
        cls.legacy_bank = BankAccount.objects.create(
            entity=cls.books, branch=None, gl_account=cls.legacy_ledger, name="Shared GTBank",
            bank_name="GTBank", account_number="0123456789", is_primary=True,
            is_primary_collection=True,
        )
        cls.move(cls.books, cls.legacy_ledger, 1_000_000, JAN_10, cls.ikeja)
        BankStatement.objects.create(
            bank_account=cls.legacy_bank, statement_date=JAN_10, opening_balance=0,
            closing_balance=1_000_000,
        )
        k = f"{BANK}."
        cls.open = cls.splitter("open")
        cls.no_result = cls.splitter("no-result", hide=[
            k + "legacy_balance", k + "unbranched_balance", k + "branch_balances",
            k + "branch_accounts", k + "split_journals", k + "branch_debts",
            k + "difference_treatment",
        ])
        cls.no_date = cls.splitter("no-date", hide=[k + "split_date"])
        cls.no_balance = cls.splitter("no-balance", hide=[k + "legacy_balance"])
        cls.no_debt_figures = cls.splitter(
            "no-debt-figures", hide=["finance.interbranch.amount"])
        cls.ro_balance = cls.splitter("ro-balance", read_only=[k + "split_opening_balance"])
        cls.ro_reference = cls.splitter("ro-reference", read_only=[
            k + "agreement_reference", k + "split_ledger_code", k + "split_ledger_name"])
        cls.ro_treatment = cls.splitter("ro-treatment", read_only=[k + "difference_treatment"])
        cls.ro_date = cls.splitter("ro-date", read_only=[k + "split_date"])
        cls.ro_flags = cls.splitter("ro-flags", read_only=[k + "is_primary"])

    @classmethod
    def move(cls, entity, account, amount, on_date, branch):
        equity = Account.objects.get(entity=entity, code="3100")
        entry = JournalEntry.objects.create(
            entity=entity, branch=branch, date=on_date, period=resolve_period(entity, on_date),
            source=JournalSource.SYSTEM, narration="Historical shared-bank movement",
        )
        for line_no, (ledger, debit, credit) in enumerate(
            ((account, amount, 0), (equity, 0, amount)), start=1,
        ):
            JournalLine.objects.create(
                entry=entry, account=ledger, debit=debit, credit=credit, line_no=line_no)
        post_journal(entry)

    @classmethod
    def splitter(cls, name, *, hide=(), read_only=()):
        user = cls.grant(
            cls.user_for(cls.tenant, f"split-{name}@fa.test"), *SPLIT_KEYS,
            tenant=cls.tenant, role_key=f"split-{name}",
        )
        role = TenantRoleTemplate.objects.get(tenant=cls.tenant, key=f"split-{name}")
        set_field_access(role, *cls.field_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        return user

    def url(self, books=None, query=""):
        return (
            f"/v1/finance/bank-accounts/{self.legacy_bank.pk}/split-by-branch/"
            f"?entity={(books or self.books).code}{query}"
        )

    def body(self, ikeja=600_000, lekki=400_000, **overrides):
        body = {
            "split_date": str(JAN_15), "agreement_reference": "BURSAR-MINUTES-2026-01-14",
            "allocations": [
                {"branch": self.ikeja.pk, "opening_balance": ikeja,
                 "bank_account_name": "Ikeja GTBank", "ledger_account_code": "1151",
                 "ledger_account_name": "Ikeja GTBank ledger", "is_primary": True,
                 "is_primary_collection": True},
                {"branch": self.lekki.pk, "opening_balance": lekki,
                 "bank_account_name": "Lekki GTBank", "ledger_account_code": "1152",
                 "ledger_account_name": "Lekki GTBank ledger", "is_primary": False,
                 "is_primary_collection": True},
            ],
        }
        body.update(overrides)
        return body

    def api(self, user):
        return TenantAPIClient(user=user)

    def still_shared(self):
        self.assertTrue(BankAccount.objects.get(pk=self.legacy_bank.pk).is_active)
        self.assertFalse(BankAccount.objects.filter(
            entity=self.books, branch__isnull=False, name__endswith="GTBank").exists())

    # -- the preview ---------------------------------------------------------- #

    def test_the_preview_drops_every_hidden_figure(self):
        full = self.api(self.open).get(self.url()).data["data"]
        self.assertEqual(
            sorted(full),
            ["bank_account_id", "branches", "legacy_balance", "split_date", "unbranched_balance"],
        )
        hidden = self.api(self.no_result).get(self.url()).data["data"]
        self.assertEqual(sorted(hidden), ["bank_account_id", "split_date"])

    def test_the_preview_drops_one_hidden_figure_and_keeps_the_rest(self):
        data = self.api(self.no_balance).get(self.url()).data["data"]
        self.assertNotIn("legacy_balance", data)
        self.assertEqual(
            [row["book_balance"] for row in data["branches"] if row["branch_id"] == self.ikeja.pk],
            [1_000_000],
        )

    def test_a_split_date_the_caller_may_not_read_cannot_be_asked_for(self):
        refused = self.api(self.no_date).get(self.url(query="&split_date=2026-01-15"))
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_read_denied")
        self.assertIn("Split date", refused.data["message"])
        plain = self.api(self.no_date).get(self.url())
        self.assertEqual(plain.status_code, 200, plain.data)
        self.assertNotIn("split_date", plain.data["data"])
        self.assertEqual(
            self.api(self.open).get(self.url(query="&split_date=2026-01-15")).status_code, 200)

    # -- the split itself ----------------------------------------------------- #

    def test_a_read_only_field_of_the_split_refuses_it_and_books_nothing(self):
        for user, label in (
            (self.ro_balance, "Agreed opening balance"),
            (self.ro_reference, "Agreement reference"),
            (self.ro_date, "Split date"),
        ):
            with self.subTest(label=label):
                response = self.api(user).post(self.url(), self.body(), format="json")
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")
                self.assertIn(label, response.data["message"])
                self.still_shared()

    def test_one_refusal_names_every_field_of_the_body_and_of_its_shares(self):
        response = self.api(self.ro_reference).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(
            sorted(response.data["error"]["detail"]),
            ["agreement_reference", "ledger_account_code", "ledger_account_name"],
        )
        for label in ("Agreement reference", "New ledger account code", "New ledger account name"):
            self.assertIn(label, response.data["message"])
        self.still_shared()

    def test_a_read_only_difference_treatment_is_refused_only_when_it_is_sent(self):
        sent = self.api(self.ro_treatment).post(
            self.url(), self.body(difference_treatment="PERMANENT_MOVE"), format="json")
        self.assertEqual(sent.status_code, 403, sent.data)
        self.assertIn("How differences are treated", sent.data["message"])
        self.still_shared()
        left_out = self.api(self.ro_treatment).post(self.url(), self.body(), format="json")
        self.assertEqual(left_out.status_code, 201, left_out.data)

    def test_an_open_role_splits_and_reads_every_result(self):
        response = self.api(self.open).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(
            sorted(data),
            ["bank_accounts", "difference_treatment", "inter_branch_transfers", "journal_ids",
             "legacy_balance", "legacy_bank_account_id"],
        )
        self.assertEqual(data["legacy_balance"], 1_000_000)
        row = data["inter_branch_transfers"][0]
        self.assertEqual(
            (row["from_branch_id"], row["to_branch_id"], row["amount"]),
            (self.ikeja.pk, self.lekki.pk, 400_000),
        )
        self.assertIn("document_number", row)

    def test_hidden_results_are_left_out_of_a_split_that_succeeded(self):
        response = self.api(self.no_result).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(sorted(response.data["data"]), ["legacy_bank_account_id"])
        self.assertTrue(BankAccount.objects.filter(
            entity=self.books, branch=self.ikeja, name="Ikeja GTBank").exists())

    def opened_row(self, request):
        from vs_finance.bank_splits import BankAccountSplitAllocationResult
        from vs_finance.views_ops.banking import _split_account_row

        return _split_account_row(request, BankAccountSplitAllocationResult(
            id=8, branch_id=self.ikeja.pk, name="Ikeja GTBank", gl_account_id=5,
            gl_account_code="1151", opening_balance=600_000, is_primary=True,
            is_primary_collection=False,
        ))

    def test_each_branch_account_opened_drops_the_share_values_its_caller_may_not_read(self):
        """A role that cannot read a share could not post it, so the row is judged directly."""
        from vs_rbac.tests.deep_payload import closed_request

        request, _ = closed_request(self.tenant, BANK)
        row = self.opened_row(request)
        self.assertEqual(row, {"id": 8, "name": "Ikeja GTBank", "branch_id": self.ikeja.pk})

    def request_hiding(self, switch):
        """A request from a user of this school who cannot read one bank account switch."""
        from rest_framework.test import APIRequestFactory

        user = self.splitter(f"hides-{switch}", hide=[f"{BANK}.{switch}"])
        request = APIRequestFactory().get("/")
        request.user = type(user).objects.get(pk=user.pk)
        request.tenant = self.tenant
        return request

    def test_each_value_of_an_account_opened_follows_its_own_switch(self):
        keys = {
            "opening_balance": "split_opening_balance",
            "gl_account_code": "split_ledger_code", "is_primary": "is_primary",
            "is_primary_collection": "is_primary_collection", "gl_account_id": "ledger_account",
        }
        for shown, switch in keys.items():
            row = self.opened_row(self.request_hiding(switch))
            with self.subTest(hidden=switch):
                self.assertNotIn(shown, row)
                self.assertEqual(len(row), 7)

    def test_an_open_role_reads_every_value_of_each_branch_account_opened(self):
        response = self.api(self.open).post(self.url(), self.body(), format="json")
        row = response.data["data"]["bank_accounts"][0]
        self.assertEqual(
            sorted(row),
            ["branch_id", "gl_account_code", "gl_account_id", "id", "is_primary",
             "is_primary_collection", "name", "opening_balance"],
        )

    def test_a_read_only_primary_flag_refuses_a_share_that_sets_it(self):
        response = self.api(self.ro_flags).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("is_primary", response.data["error"]["detail"])
        self.still_shared()

    def test_a_debt_between_branches_obeys_the_inter_branch_amount_switch_too(self):
        response = self.api(self.no_debt_figures).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        row = response.data["data"]["inter_branch_transfers"][0]
        self.assertNotIn("amount", row)
        self.assertEqual(row["from_branch_id"], self.ikeja.pk)
        self.assertEqual(row["to_branch_id"], self.lekki.pk)
        self.assertIn("document_number", row)

    def test_the_sentence_about_mismatched_shares_names_the_balance_only_to_its_reader(self):
        from vs_finance.money import format_naira

        body = self.body(ikeja=100_000, lekki=100_000)
        named = self.api(self.open).post(self.url(), body, format="json")
        self.assertEqual(named.status_code, 422, named.data)
        self.assertIn(format_naira(1_000_000), named.data["message"])
        plain = self.api(self.no_balance).post(self.url(), body, format="json")
        self.assertEqual(plain.status_code, 422, plain.data)
        self.assertNotIn(format_naira(1_000_000), plain.data["message"])
        self.assertIn("do not add up", plain.data["message"])
        self.still_shared()

    # -- reach and tenants ---------------------------------------------------- #

    def test_a_role_in_another_school_reaches_nothing_of_this_account(self):
        rival = self.grant(
            self.user_for(self.rival_tenant, "split-rival@fa.test"), *SPLIT_KEYS,
            tenant=self.rival_tenant, role_key="split-rival",
        )
        for response in (
            self.api(rival).get(self.url(self.books)),
            self.api(rival).post(self.url(self.books), self.body(), format="json"),
        ):
            self.assertIn(response.status_code, (403, 404), response.data)
        self.still_shared()

    def test_a_branch_pinned_caller_is_refused_before_any_field_is_judged(self):
        pinned = self.grant(
            self.user_for(self.tenant, "split-pinned@fa.test"), *SPLIT_KEYS,
            tenant=self.tenant, role_key="split-pinned", branch=self.ikeja,
        )
        response = self.api(pinned).post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("school-wide administrator", str(response.data))
        self.still_shared()
