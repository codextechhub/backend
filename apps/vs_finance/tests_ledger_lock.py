"""Hand-typed journals stay off the accounts a sub-ledger keeps.

Corona's bursar, Mrs Okafor, can still type an accrual. What she can no longer do
is type a line into accounts receivable, accounts payable, a bank account's ledger
or VAT payable, because each of those equals the sum of the documents behind it and
a typed line would make the two disagree for good. The refusal names the document
to use instead. The documents themselves post exactly as before.

Money into or out of the bank with no customer or supplier behind it (owner
capital, a loan, interest) is a bank transaction: it names the bank account, carries
that account's branch and posts through the banking service. Money between two of
Ikeja's own accounts (Zenith to GTBank) is a transfer; money between Ikeja's and
Lekki's accounts is an inter-branch transfer, which is not built, and is refused.

Both are transactions, so they follow the branch rules every transaction does. A
branch-bound bursar reads and moves only their own branches' money, and an account
not yet given a branch, at a school with several, moves nothing at all: nobody
can say whose money it holds. At a school with one branch such an account is that
branch's.
"""
from __future__ import annotations

import datetime
import itertools

from core.test_utils import TenantAPIClient

from .constants import DocumentStatus, JournalSource
from .control_accounts import ControlAccountLockedError
from .banking import match_line
from .models import (
    Account,
    BankAccount,
    BankStatementLine,
    BankTransaction,
    BankTransfer,
    Customer,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
)
from .banking import import_statement_lines
from .posting import (
    create_direct_entry,
    journal_reversal_action,
    post_direct_entry,
    post_journal,
    resolve_period,
)
from .receivables import post_invoice
from .reports import _account_gl_net
from .tests_branch_scope import _FinanceBranchFixture

JAN_15 = datetime.date(2026, 1, 15)
_roles = itertools.count(1)


class _LockFixture(_FinanceBranchFixture):
    """Corona's books with an Ikeja operations account on its own ledger."""

    def setUp(self):
        super().setUp()
        e = self.books
        self.ikeja_ledger = Account.objects.create(
            entity=e, code="1151", name="Ikeja GTBank",
            account_type=self.acc("1100").account_type, is_postable=True,
        )
        self.ikeja_bank = BankAccount.objects.create(
            entity=e, name="Ikeja GTBank", branch=self.ikeja, gl_account=self.ikeja_ledger,
        )

    def acc(self, code):
        return Account.objects.get(entity=self.books, code=code)

    def bank(self, code, name, branch):
        ledger = Account.objects.create(
            entity=self.books, code=code, name=name,
            account_type=self.acc("1100").account_type, is_postable=True,
        )
        return BankAccount.objects.create(
            entity=self.books, name=name, branch=branch, gl_account=ledger,
        )

    def journal(self, pairs, *, source=JournalSource.MANUAL):
        entry = JournalEntry.objects.create(
            entity=self.books, date=JAN_15, period=resolve_period(self.books, JAN_15),
            narration="typed by hand", source=source,
        )
        for line_no, (account, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=account, debit=debit, credit=credit, line_no=line_no,
            )
        return entry


class HandJournalLockTests(_LockFixture):
    def test_a_typed_journal_to_a_control_account_is_refused(self):
        for code_or_account, document in (
            ("2100", "vendor bill"), ("1200", "invoice"), ("2200", "tax filing"),
        ):
            with self.subTest(account=code_or_account):
                account = self.acc(code_or_account)
                entry = self.journal([(self.acc("5300"), 10_000, 0), (account, 0, 10_000)]
                                     if account.account_type != "ASSET"
                                     else [(account, 10_000, 0), (self.acc("4100"), 0, 10_000)])
                with self.assertRaisesMessage(ControlAccountLockedError, document):
                    post_journal(entry)
                entry.refresh_from_db()
                self.assertEqual(entry.status, DocumentStatus.DRAFT)

    def test_a_typed_journal_to_a_bank_ledger_is_refused_and_names_the_bank_transaction(self):
        entry = self.journal([(self.ikeja_ledger, 50_000, 0), (self.acc("3100"), 0, 50_000)])

        with self.assertRaisesMessage(ControlAccountLockedError, "bank transaction"):
            post_journal(entry)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 0)

    def test_a_direct_entry_to_a_control_account_is_refused_before_it_exists(self):
        before = JournalEntry.objects.filter(entity=self.books).count()

        with self.assertRaises(ControlAccountLockedError):
            create_direct_entry(
                self.books, lines=[("5300", 10_000, 0), ("2100", 0, 10_000)], date=JAN_15,
            )
        self.assertEqual(JournalEntry.objects.filter(entity=self.books).count(), before)

    def test_a_direct_entry_between_ordinary_accounts_posts_as_manual(self):
        entry = post_direct_entry(
            self.books, lines=[("5300", 10_000, 0), ("2400", 0, 10_000)], date=JAN_15,
            narration="Accrued reimbursements",
        )

        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(entry.source, JournalSource.MANUAL)
        self.assertEqual(_account_gl_net(self.acc("2400")), 10_000)

    def test_a_true_opening_balance_is_still_marked_opening(self):
        entry = post_direct_entry(
            self.books, lines=[("1500", 40_000, 0), ("3200", 0, 40_000)], date=JAN_15,
            narration="Furniture owned when the books began", opening=True,
        )

        self.assertEqual(entry.source, JournalSource.OPENING)

    def test_sub_ledger_documents_still_post_to_control_accounts(self):
        customer = Customer.objects.create(
            entity=self.books, code="ADA", name="Parent Ada", branch=self.ikeja,
            receivable_account=self.acc("1200"),
        )
        invoice = Invoice.objects.create(
            entity=self.books, customer=customer, branch=self.ikeja,
            invoice_date=JAN_15, due_date=JAN_15,
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=100_000,
            revenue_account=self.acc("4100"),
        )

        post_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        self.assertEqual(_account_gl_net(self.acc("1200")), 100_000)
        banked = self.journal(
            [(self.ikeja_ledger, 20_000, 0), (self.acc("4100"), 0, 20_000)],
            source=JournalSource.BANK,
        )
        post_journal(banked)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 20_000)


class BankTransactionTests(_LockFixture):
    KEYS = (
        "finance.banktransaction.view", "finance.banktransaction.create",
        "finance.banktransaction.reverse",
    )

    def bursar(self, *branches):
        user = self.user_for(self.tenant, f"bursar-{next(_roles)}@corona.test")
        for branch in branches:
            self.grant(user, *self.KEYS, tenant=self.tenant,
                       role_key=f"bursar-{next(_roles)}", branch=branch)
        return TenantAPIClient(user=user)

    def url(self, path=""):
        return f"/v1/finance/bank-transactions/{path}?entity={self.books.code}"

    def body(self, **extra):
        return {
            "bank_account": self.ikeja_bank.pk, "direction": "IN", "amount": 5_000_000,
            "counter_account": "3100", "transaction_date": "2026-01-15",
            "narration": "Owner capital", **extra,
        }

    def test_owner_capital_posts_to_the_bank_on_its_branch(self):
        response = self.bursar(self.ikeja).post(self.url(), self.body(), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        txn = BankTransaction.objects.get()
        self.assertEqual((txn.status, txn.branch_id), (DocumentStatus.POSTED, self.ikeja.pk))
        self.assertEqual(txn.journal.source, JournalSource.BANK)
        self.assertEqual(txn.journal.branch_id, self.ikeja.pk)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 5_000_000)
        self.assertEqual(_account_gl_net(self.acc("3100")), 5_000_000)
        self.assertEqual(journal_reversal_action(txn.journal)["document_type"], "BANK_TRANSACTION")

    def test_money_out_credits_the_bank(self):
        response = self.bursar(self.ikeja).post(
            self.url(), self.body(direction="OUT", amount=200_000, counter_account="5300",
                                  narration="Owner drawings"), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), -200_000)

    def test_a_control_account_cannot_be_the_other_side(self):
        response = self.bursar(self.ikeja).post(
            self.url(), self.body(counter_account="2100"), format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("vendor bill", str(response.data))
        self.assertFalse(BankTransaction.objects.exists())

    def test_lekki_cannot_move_money_in_ikejas_account(self):
        response = self.bursar(self.lekki).post(self.url(), self.body(), format="json")

        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(BankTransaction.objects.exists())

    def test_a_voided_transaction_leaves_the_bank_where_it_was(self):
        client = self.bursar(self.ikeja)
        created = client.post(self.url(), self.body(), format="json")
        pk = created.data["data"]["id"]

        response = client.post(self.url(f"{pk}/void/"), {"date": "2026-01-20"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(BankTransaction.objects.get().status, DocumentStatus.REVERSED)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 0)

    def test_lekki_neither_reads_nor_voids_ikejas_transaction(self):
        created = self.bursar(self.ikeja).post(self.url(), self.body(), format="json")
        pk = created.data["data"]["id"]
        lekki = self.bursar(self.lekki)

        listed = lekki.get(self.url()).data["data"]
        detail = lekki.get(self.url(f"{pk}/"))
        voided = lekki.post(self.url(f"{pk}/void/"), {"date": "2026-01-20"}, format="json")

        self.assertEqual(listed, [])
        self.assertEqual((detail.status_code, voided.status_code), (404, 404))
        self.assertEqual(BankTransaction.objects.get().status, DocumentStatus.POSTED)

    def test_an_unbranched_account_moves_nothing_at_a_school_with_several_branches(self):
        """Mr Eze covers the whole school; the UBA account has no branch, so it is refused."""
        shared = self.bank("1156", "School UBA", None)

        whole_school = self.bursar(None).post(
            self.url(), self.body(bank_account=shared.pk), format="json")
        ikeja = self.bursar(self.ikeja).post(
            self.url(), self.body(bank_account=shared.pk), format="json")

        self.assertEqual(whole_school.status_code, 400, whole_school.data)
        self.assertIn("School UBA has not been given a branch", str(whole_school.data))
        self.assertEqual(ikeja.status_code, 404, ikeja.data)
        self.assertFalse(BankTransaction.objects.exists())

    def test_a_transaction_not_yet_given_a_branch_is_read_only_by_the_whole_school(self):
        entry = BankTransaction.objects.create(
            entity=self.books, bank_account=self.ikeja_bank, direction="IN", amount=1_000,
            counter_account=self.acc("3100"), transaction_date=JAN_15, narration="Before branches",
        )

        ikeja = self.bursar(self.ikeja).get(self.url(f"{entry.pk}/"))
        whole_school = self.bursar(None).get(self.url(f"{entry.pk}/"))

        self.assertEqual(ikeja.status_code, 404, ikeja.data)
        self.assertEqual(whole_school.status_code, 200, whole_school.data)

    def test_at_a_one_branch_school_an_unbranched_account_is_its_branchs(self):
        ledger = Account.objects.create(
            entity=self.solo_books, code="1151", name="Main GTBank",
            account_type=Account.objects.get(entity=self.solo_books, code="1100").account_type,
            is_postable=True,
        )
        bank = BankAccount.objects.create(
            entity=self.solo_books, name="Main GTBank", branch=None, gl_account=ledger,
        )
        user = self.user_for(self.solo_tenant, f"solo-{next(_roles)}@solo.test")
        self.grant(user, *self.KEYS, tenant=self.solo_tenant, role_key=f"solo-{next(_roles)}",
                   branch=self.solo_main)

        response = TenantAPIClient(user=user).post(
            f"/v1/finance/bank-transactions/?entity={self.solo_books.code}",
            self.body(bank_account=bank.pk), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        txn = BankTransaction.objects.get()
        self.assertEqual((txn.branch_id, txn.journal.branch_id), (self.solo_main.pk,) * 2)

    def test_a_transaction_whose_account_moved_branch_does_not_post(self):
        """Approved for Ikeja, then the account was given to Lekki: the money stays put."""
        from .banking import post_bank_transaction
        from .exceptions import PostingError

        txn = BankTransaction.objects.create(
            entity=self.books, branch=self.ikeja, bank_account=self.ikeja_bank, direction="IN",
            amount=1_000, counter_account=self.acc("3100"), transaction_date=JAN_15,
            narration="Owner capital",
        )
        BankAccount.objects.filter(pk=self.ikeja_bank.pk).update(branch=self.lekki)
        txn.refresh_from_db()

        with self.assertRaisesMessage(PostingError, "belongs to Ikeja Branch"):
            post_bank_transaction(txn)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 0)


class BankTransferTests(_LockFixture):
    """Mrs Okafor moves N2m from Ikeja's Zenith account into Ikeja's GTBank account."""

    KEYS = (
        "finance.banktransfer.view", "finance.banktransfer.create",
        "finance.banktransfer.reverse",
    )

    def setUp(self):
        super().setUp()
        self.ikeja_zenith = self.bank("1152", "Ikeja Zenith", self.ikeja)
        self.lekki_access = self.bank("1153", "Lekki Access", self.lekki)
        self.shared_uba = self.bank("1154", "School UBA", None)
        self.shared_fcmb = self.bank("1155", "School FCMB", None)

    def bursar(self, *branches):
        user = self.user_for(self.tenant, f"transfer-{next(_roles)}@corona.test")
        for branch in branches:
            self.grant(user, *self.KEYS, tenant=self.tenant,
                       role_key=f"transfer-{next(_roles)}", branch=branch)
        return TenantAPIClient(user=user)

    def url(self, path=""):
        return f"/v1/finance/bank-transfers/{path}?entity={self.books.code}"

    def body(self, source, target, **extra):
        return {
            "from_account": source.pk, "to_account": target.pk, "amount": 2_000_000_00,
            "transfer_date": "2026-01-15", "narration": "Fund the GTBank account", **extra,
        }

    def test_a_transfer_moves_money_between_ikejas_own_accounts(self):
        response = self.bursar(self.ikeja).post(
            self.url(), self.body(self.ikeja_zenith, self.ikeja_bank), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        transfer = BankTransfer.objects.get()
        self.assertEqual((transfer.status, transfer.branch_id), (DocumentStatus.POSTED, self.ikeja.pk))
        self.assertEqual(transfer.journal.source, JournalSource.BANK)
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 2_000_000_00)
        self.assertEqual(_account_gl_net(self.ikeja_zenith.gl_account), -2_000_000_00)
        self.assertEqual(journal_reversal_action(transfer.journal)["document_type"], "BANK_TRANSFER")

    def test_both_sides_reach_their_own_reconciliation(self):
        from .banking import _unmatched_gl_lines

        self.bursar(self.ikeja).post(
            self.url(), self.body(self.ikeja_zenith, self.ikeja_bank), format="json")
        transfer = BankTransfer.objects.get()

        received = transfer.journal.lines.get(account=self.ikeja_ledger)
        sent = transfer.journal.lines.get(account=self.ikeja_zenith.gl_account)
        self.assertEqual([line.pk for line in _unmatched_gl_lines(self.ikeja_bank)], [received.pk])
        self.assertEqual([line.pk for line in _unmatched_gl_lines(self.ikeja_zenith)], [sent.pk])

    def test_money_between_branches_is_refused_as_an_inter_branch_transfer(self):
        response = self.bursar(self.ikeja, self.lekki).post(
            self.url(), self.body(self.ikeja_bank, self.lekki_access), format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("inter-branch transfer", str(response.data))
        self.assertFalse(BankTransfer.objects.exists())

    def test_accounts_not_yet_given_a_branch_move_nothing(self):
        """No school-wide transfer: Mrs Okafor cannot see them, Mr Eze is told to place them."""
        ikeja = self.bursar(self.ikeja).post(
            self.url(), self.body(self.shared_uba, self.shared_fcmb), format="json")
        whole_school = self.bursar(None).post(
            self.url(), self.body(self.shared_uba, self.shared_fcmb), format="json")

        self.assertEqual(ikeja.status_code, 404, ikeja.data)
        self.assertEqual(whole_school.status_code, 400, whole_school.data)
        self.assertIn("School UBA has not been given a branch", str(whole_school.data))
        self.assertFalse(BankTransfer.objects.exists())

    def test_an_unbranched_account_is_not_one_of_ikejas_own(self):
        response = self.bursar(None).post(
            self.url(), self.body(self.shared_uba, self.ikeja_bank), format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("no branch yet", str(response.data))
        self.assertFalse(BankTransfer.objects.exists())

    def test_lekki_neither_reads_nor_voids_ikejas_transfer(self):
        created = self.bursar(self.ikeja).post(
            self.url(), self.body(self.ikeja_zenith, self.ikeja_bank), format="json")
        pk = created.data["data"]["id"]
        lekki = self.bursar(self.lekki)

        listed = lekki.get(self.url()).data["data"]
        detail = lekki.get(self.url(f"{pk}/"))
        voided = lekki.post(self.url(f"{pk}/void/"), {"date": "2026-01-20"}, format="json")

        self.assertEqual(listed, [])
        self.assertEqual((detail.status_code, voided.status_code), (404, 404))
        self.assertEqual(BankTransfer.objects.get().status, DocumentStatus.POSTED)

    def test_a_transfer_is_voided_until_a_statement_line_is_matched_to_it(self):
        client = self.bursar(self.ikeja)
        first = client.post(
            self.url(), self.body(self.ikeja_zenith, self.ikeja_bank), format="json")
        second = client.post(
            self.url(), self.body(self.ikeja_zenith, self.ikeja_bank, amount=500_000),
            format="json")
        matched = BankTransfer.objects.get(pk=second.data["data"]["id"])
        statement_line = import_statement_lines(self.ikeja_bank, [
            {"txn_date": JAN_15, "amount": 500_000},
        ])[1][0]
        match_line(statement_line, matched.journal.lines.get(account=self.ikeja_ledger))

        voided = client.post(self.url(f"{first.data['data']['id']}/void/"),
                             {"date": "2026-01-20"}, format="json")
        refused = client.post(self.url(f"{matched.pk}/void/"), {"date": "2026-01-20"},
                              format="json")

        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(refused.status_code, 422, refused.data)
        matched.refresh_from_db()
        self.assertEqual(matched.status, DocumentStatus.POSTED)
        self.assertTrue(BankStatementLine.objects.filter(matched_line__isnull=False).exists())
        self.assertEqual(_account_gl_net(self.ikeja_ledger), 500_000)
