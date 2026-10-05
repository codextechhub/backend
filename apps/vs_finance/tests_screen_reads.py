"""Reads the finance screens need beside the documents they already show.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Each class
below answers one question a screen asks:

* how a shared bank account would split, before anyone agrees a share;
* which income a credit note or concession at Lekki took back from Ikeja;
* what a pupil's receivable move carried, and who owes whom for it;
* whose customer a code names at another branch, for a receipt held for it;
* what the close check says when two branches' books disagree, in words;
* that re-saving a tax code keeps what the request does not name;
* which months the fiscal calendar leaves uncovered;
* which ledger lines a tax return declares, line by line;
* whether the maker of a journal has left;
* that the journal and invoice count tabs agree with their lists over archived years;
* where each branch stands in each fiscal period and year.

Every read keeps to the reader's branches, refuses a reader without the key,
and costs the same number of queries for a page of one row as for a page of
several.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.test_utils import TenantAPIClient

from .constants import (
    CreditNoteKind,
    DocumentStatus,
    InterBranchTransferKind,
    InvoicePaymentStatus,
    JournalSource,
    PeriodStatus,
)
from .inter_branch import book_goods_transfer, inter_branch_close_check, transfer_open_receivables
from .models import (
    Account,
    BankAccount,
    BranchFiscalPeriod,
    CostCenter,
    CreditNote,
    CreditNoteLine,
    FiscalPeriod,
    FiscalYear,
    InterBranchTransfer,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    Payment,
    TaxCode,
    TaxObligation,
)
from .posting import post_journal, resolve_period
from .receivables import post_invoice, post_payment
from .tests_inter_branch import JAN_15, JAN_20, _InterBranchFixture, _TermFixture

JAN_10 = datetime.date(2026, 1, 10)
JAN_31 = datetime.date(2026, 1, 31)


class _ReadsFixture(_InterBranchFixture):
    """People with exactly the keys a screen needs, at one branch or across the tenant."""

    @classmethod
    def person(cls, *keys, branch=None, tenant=None):
        """A user holding ``keys`` at ``branch``, or across the tenant when it is ``None``."""
        return cls.bursar(branch, tenant=tenant, keys=keys)

    def as_(self, user):
        return TenantAPIClient(user=user)

    def rows(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, "data", response))
        return response.data["data"]

    def queries(self, client, path):
        """How many queries one GET of ``path`` costs, after it answered 200."""
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path)
        self.assertEqual(response.status_code, 200, response.data)
        return len(captured.captured_queries)


# --------------------------------------------------------------------------- #
# 1. Splitting a shared bank account                                          #
# --------------------------------------------------------------------------- #

class BankSplitPreviewTests(_ReadsFixture):
    """Corona's old GTBank holds N4,000: Ikeja's entries come to N5,000, Lekki's to minus N1,000."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.legacy_ledger = Account.objects.create(
            entity=cls.books, parent=Account.objects.get(entity=cls.books, code="1100"),
            code="1150", name="Shared GTBank ledger", account_type="ASSET",
            normal_balance="DEBIT", is_postable=True,
        )
        cls.legacy = BankAccount.objects.create(
            entity=cls.books, branch=None, gl_account=cls.legacy_ledger, name="Shared GTBank",
        )
        cls.movement(cls.books, cls.legacy_ledger, 500_000, cls.ikeja)
        cls.movement(cls.books, cls.legacy_ledger, -100_000, cls.lekki)
        cls.whole = cls.person("finance.bankaccount.view", "finance.bankaccount.update")
        cls.ikeja_only = cls.person("finance.bankaccount.view", branch=cls.ikeja)

    @classmethod
    def movement(cls, books, ledger, amount, branch):
        equity = Account.objects.get(entity=books, code="3100")
        entry = JournalEntry.objects.create(
            entity=books, branch=branch, date=JAN_10, period=resolve_period(books, JAN_10),
            source=JournalSource.SYSTEM, narration="Shared bank movement",
        )
        rows = ((ledger, amount, 0), (equity, 0, amount)) if amount > 0 \
            else ((equity, -amount, 0), (ledger, 0, -amount))
        for line_no, (account, debit, credit) in enumerate(rows, start=1):
            JournalLine.objects.create(entry=entry, account=account, debit=debit, credit=credit,
                                       line_no=line_no)
        post_journal(entry)

    def preview_url(self, bank=None, books=None, split_date="2026-01-15"):
        return self.url(f"bank-accounts/{(bank or self.legacy).pk}/split-by-branch/"
                        f"?split_date={split_date}", books)

    def test_each_branch_shows_its_own_book_balance_on_the_shared_account(self):
        data = self.rows(self.as_(self.whole).get(self.preview_url()))

        self.assertEqual((data["legacy_balance"], data["unbranched_balance"]), (400_000, 0))
        self.assertEqual(data["bank_account_id"], self.legacy.pk)
        self.assertEqual(data["branches"], [
            {"branch_id": self.ikeja.pk, "branch_name": "Ikeja Branch", "book_balance": 500_000},
            {"branch_id": self.lekki.pk, "branch_name": "Lekki Branch", "book_balance": -100_000},
            {"branch_id": self.yaba.pk, "branch_name": "Yaba Branch", "book_balance": 0},
        ])

    def test_the_split_measures_its_debts_against_the_same_figures(self):
        from .bank_splits import split_shared_bank_account
        from .inter_branch import pair_balances

        preview = {row["branch_id"]: row["book_balance"]
                   for row in self.rows(self.as_(self.whole).get(self.preview_url()))["branches"]}
        allocations = [
            {"branch": branch, "opening_balance": share, "bank_account_name": f"{branch.name} GTBank",
             "ledger_account_code": code, "ledger_account_name": f"{branch.name} GTBank ledger",
             "is_primary": False, "is_primary_collection": False}
            for branch, share, code in ((self.ikeja, 250_000, "1154"), (self.lekki, 150_000, "1155"))
        ]
        split_shared_bank_account(self.legacy, allocations, split_date=JAN_15,
                                  agreement_reference="MINUTES-1")

        (pair,) = pair_balances(self.books)["pairs"]
        self.assertEqual((pair["owed_by"]["id"], pair["owed_to"]["id"]), (self.lekki.pk, self.ikeja.pk))
        self.assertEqual(pair["amount"], preview[self.ikeja.pk] - 250_000)

    def test_a_date_before_the_movements_shows_nothing_yet(self):
        data = self.rows(self.as_(self.whole).get(self.preview_url(split_date="2026-01-05")))

        self.assertEqual(data["legacy_balance"], 0)
        self.assertEqual({row["book_balance"] for row in data["branches"]}, {0})

    def test_a_branch_bound_reader_is_refused(self):
        response = self.as_(self.ikeja_only).get(self.preview_url())

        self.assertEqual(response.status_code, 403, response.data)

    def test_an_account_that_already_has_a_branch_is_not_shared(self):
        response = self.as_(self.whole).get(self.preview_url(bank=self.ikeja_bank))

        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("already belongs to a branch", str(response.data))

    def test_a_future_date_is_refused(self):
        response = self.as_(self.whole).get(self.preview_url(split_date="2099-01-01"))

        self.assertEqual(response.status_code, 400, response.data)

    def test_another_tenants_account_is_not_found(self):
        rival_ledger = Account.objects.get(entity=self.rival_books, code="1100")
        rival = BankAccount.objects.create(entity=self.rival_books, gl_account=rival_ledger, name="Rival")

        response = self.as_(self.whole).get(self.preview_url(bank=rival))

        self.assertEqual(response.status_code, 404, response.data)

    def test_a_one_branch_school_lists_its_only_branch(self):
        ledger = Account.objects.create(
            entity=self.solo_books, parent=Account.objects.get(entity=self.solo_books, code="1100"),
            code="1150", name="Old ledger", account_type="ASSET", normal_balance="DEBIT",
            is_postable=True,
        )
        old = BankAccount.objects.create(entity=self.solo_books, gl_account=ledger, name="Old")
        self.movement(self.solo_books, ledger, 70_000, self.solo_main)
        reader = self.person("finance.bankaccount.view", tenant=self.solo_tenant)

        data = self.rows(self.as_(reader).get(self.preview_url(bank=old, books=self.solo_books)))

        self.assertEqual(data["branches"], [
            {"branch_id": self.solo_main.pk, "branch_name": "Main Branch", "book_balance": 70_000},
        ])


# --------------------------------------------------------------------------- #
# 2. Income given back by a credit note or concession                         #
# --------------------------------------------------------------------------- #

class IncomeGivenBackLinkTests(_ReadsFixture):
    """Tunde's 100k textbook bill (plus 7.5k VAT) is raised at Ikeja and moves to Lekki with him."""

    AR_KEYS = ("finance.creditnote.view", "finance.concession.view", "finance.interbranch.view")

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vat = TaxCode.objects.get(entity=cls.books, code="VAT-STD")
        cls.revenue = Account.objects.get(entity=cls.books, code="4100")
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.textbooks = cls.taxed_invoice(cls.tunde, cls.ikeja)
        cls.second = cls.taxed_invoice(cls.tunde, cls.ikeja)
        transfer_open_receivables(cls.tunde, cls.ikeja, cls.lekki, None, move_date=JAN_15)
        cls.lekki_reader = cls.person(*cls.AR_KEYS, branch=cls.lekki)
        cls.yaba_reader = cls.person(*cls.AR_KEYS, branch=cls.yaba)

    @classmethod
    def taxed_invoice(cls, customer, branch):
        invoice = Invoice.objects.create(
            entity=cls.books, customer=customer, branch=branch,
            invoice_date=JAN_10, due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(invoice=invoice, line_no=1, quantity=1, unit_price=100_000,
                                   revenue_account=cls.revenue, tax_code=cls.vat)
        post_invoice(invoice)
        return invoice

    def credit(self, invoice, amount=100_000):
        from .credit_notes import post_credit_note

        invoice = Invoice.objects.get(pk=invoice.pk)
        note = CreditNote.objects.create(
            entity=self.books, customer=invoice.customer, branch=invoice.branch,
            kind=CreditNoteKind.CREDIT, note_date=JAN_20, invoice=invoice, reason="Never delivered",
        )
        CreditNoteLine.objects.create(note=note, line_no=1, quantity=1, unit_price=amount,
                                      revenue_account=self.revenue, tax_code=self.vat)
        post_credit_note(note)
        note.refresh_from_db()
        return note

    def given_back(self):
        return InterBranchTransfer.objects.get(kind=InterBranchTransferKind.INCOME_GIVEN_BACK)

    def test_a_credit_note_names_the_income_ikeja_gave_back(self):
        note = self.credit(self.textbooks)

        data = self.rows(self.as_(self.lekki_reader).get(self.url(f"credit-notes/{note.pk}/")))

        transfer = self.given_back()
        self.assertEqual(data["income_given_back"], [{
            "id": transfer.pk, "document_number": transfer.document_number,
            "to_branch_id": self.ikeja.pk, "to_branch_name": "Ikeja Branch",
            "amount": 107_500, "status": DocumentStatus.POSTED,
        }])

    def test_a_list_reads_every_notes_income_given_back_in_one_query(self):
        reader = self.as_(self.lekki_reader)
        self.credit(self.textbooks)
        one = self.queries(reader, self.url("credit-notes/"))
        self.credit(self.second, amount=40_000)

        two = self.queries(reader, self.url("credit-notes/"))
        rows = self.rows(reader.get(self.url("credit-notes/")))

        self.assertEqual(one, two)
        self.assertEqual(sorted(row["income_given_back"][0]["amount"] for row in rows), [43_000, 107_500])

    def test_a_concession_names_it_too_and_a_void_shows_it_reversed(self):
        from .installments import post_concession
        from .models import Concession
        from .voids import void_concession

        concession = Concession.objects.create(
            entity=self.books, customer=self.tunde, invoice=Invoice.objects.get(pk=self.textbooks.pk),
            branch=self.lekki, concession_date=JAN_20, amount=30_000,
        )
        post_concession(concession)
        void_concession(concession, date=JAN_20)

        data = self.rows(self.as_(self.lekki_reader).get(self.url(f"concessions/{concession.pk}/")))
        listed = self.rows(self.as_(self.lekki_reader).get(self.url("concessions/")))

        self.assertEqual([(row["amount"], row["status"]) for row in data["income_given_back"]],
                         [(30_000, DocumentStatus.REVERSED)])
        self.assertEqual(listed[0]["income_given_back"], data["income_given_back"])

    def test_a_note_on_a_bill_that_never_moved_gave_nothing_back(self):
        yinka = self.customer(self.books, "YINKA", self.lekki)
        note = self.credit(self.taxed_invoice(yinka, self.lekki))

        data = self.rows(self.as_(self.lekki_reader).get(self.url(f"credit-notes/{note.pk}/")))

        self.assertEqual(data["income_given_back"], [])

    def test_the_register_finds_the_transfers_of_one_adjusting_journal(self):
        note = self.credit(self.textbooks)
        self.credit(self.second, amount=40_000)
        path = self.url(f"inter-branch-transfers/?adjustment={note.journal_id}")

        lekki = self.rows(self.as_(self.lekki_reader).get(path))
        yaba = self.rows(self.as_(self.yaba_reader).get(path))
        bad = self.as_(self.lekki_reader).get(self.url("inter-branch-transfers/?adjustment=x"))

        self.assertEqual([(row["adjustment_entry_id"], row["amount"]) for row in lekki],
                         [(note.journal_id, 107_500)])
        self.assertEqual(yaba, [])
        self.assertEqual(bad.status_code, 400)


# --------------------------------------------------------------------------- #
# 3. What a receivable move carried                                           #
# --------------------------------------------------------------------------- #

class MovedItemsTests(_ReadsFixture):
    """Tunde moves owing a 100k bill and a 25k debit note, with 40k of an unapplied receipt."""

    @classmethod
    def setUpTestData(cls):
        from .credit_notes import post_credit_note

        super().setUpTestData()
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.bill = cls.posted_invoice(cls.tunde, cls.ikeja)
        cls.debit_note = CreditNote.objects.create(
            entity=cls.books, customer=cls.tunde, branch=cls.ikeja, kind=CreditNoteKind.DEBIT,
            note_date=datetime.date(2026, 1, 11), reason="Lab fee missed",
        )
        CreditNoteLine.objects.create(note=cls.debit_note, line_no=1, quantity=1, unit_price=25_000,
                                      revenue_account=Account.objects.get(entity=cls.books, code="4100"))
        post_credit_note(cls.debit_note)
        cls.advance = Payment.objects.create(
            entity=cls.books, branch=cls.ikeja, customer=cls.tunde, payment_date=datetime.date(2026, 1, 12),
            amount=40_000, deposit_account=cls.ikeja_bank.gl_account,
        )
        post_payment(cls.advance, auto_allocate=False)
        moved = transfer_open_receivables(cls.tunde, cls.ikeja, cls.lekki, None, move_date=JAN_15)
        cls.move = InterBranchTransfer.objects.get(pk=moved.transfer_id)
        cls.ikeja_reader = cls.person("finance.interbranch.view", branch=cls.ikeja)
        cls.lekki_reader = cls.person("finance.interbranch.view", branch=cls.lekki)
        cls.yaba_reader = cls.person("finance.interbranch.view", branch=cls.yaba)

    def test_both_branches_see_every_document_the_move_carried(self):
        for reader in (self.ikeja_reader, self.lekki_reader):
            data = self.rows(self.as_(reader).get(self.url(f"inter-branch-transfers/{self.move.pk}/")))

            self.debit_note.refresh_from_db()
            self.assertEqual(sorted(data["moved_items"], key=lambda row: row["kind"]), [
                {"kind": "DEBIT_NOTE", "document_number": self.debit_note.document_number,
                 "invoice_id": None, "note_id": self.debit_note.pk, "payment_id": None,
                 "amount": 25_000, "deferred_amount": 0},
                {"kind": "INVOICE", "document_number": self.bill.document_number,
                 "invoice_id": self.bill.pk, "note_id": None, "payment_id": None,
                 "amount": 100_000, "deferred_amount": 0},
                {"kind": "RECEIPT_CREDIT", "document_number": self.advance.document_number,
                 "invoice_id": None, "note_id": None, "payment_id": self.advance.pk,
                 "amount": 40_000, "deferred_amount": 0},
            ])
            self.assertEqual(data["net_owed"], {
                "amount": 85_000,
                "owed_by": {"id": self.lekki.pk, "name": "Lekki Branch"},
                "owed_to": {"id": self.ikeja.pk, "name": "Ikeja Branch"},
            })

    def test_a_branch_outside_the_move_cannot_open_it(self):
        response = self.as_(self.yaba_reader).get(self.url(f"inter-branch-transfers/{self.move.pk}/"))

        self.assertEqual(response.status_code, 404)

    def test_the_register_reads_moved_items_for_a_whole_page_at_once(self):
        reader = self.as_(self.person("finance.interbranch.view"))
        one = self.queries(reader, self.url("inter-branch-transfers/"))
        other = self.customer(self.books, "KEMI", self.ikeja)
        self.posted_invoice(other, self.ikeja)
        transfer_open_receivables(other, self.ikeja, self.yaba, None, move_date=JAN_15)

        two = self.queries(reader, self.url("inter-branch-transfers/"))

        self.assertEqual(one, two)

    def test_a_cash_transfer_carries_no_items(self):
        response = self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["data"]["moved_items"], response.data["data"]["net_owed"]),
                         ([], None))


class MovedUnearnedIncomeTests(_ReadsFixture, _TermFixture):
    """Tunde's 400k term moves on 25 January: 300k of it not yet earned, so Lekki owes Ikeja 100k."""

    def test_the_net_takes_the_unearned_income_off_what_is_owed(self):
        moved = transfer_open_receivables(
            self.tunde, self.ikeja, self.lekki, None, move_date=datetime.date(2026, 1, 25))
        reader = self.person("finance.interbranch.view")

        data = self.rows(self.as_(reader).get(self.url(f"inter-branch-transfers/{moved.transfer_id}/")))

        (item,) = data["moved_items"]
        self.assertEqual((item["kind"], item["amount"], item["deferred_amount"]),
                         ("INVOICE", 400_000, 300_000))
        self.assertEqual((data["net_owed"]["amount"], data["net_owed"]["owed_by"]["id"]),
                         (100_000, self.lekki.pk))


# --------------------------------------------------------------------------- #
# 4. Whose customer a code names at another branch                            #
# --------------------------------------------------------------------------- #

class HeldReceiptCustomerLookupTests(_ReadsFixture):
    """Mrs Adeyemi pays her son's Lekki fees into Ikeja's account."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.adeyemi = cls.customer(cls.books, "ADE01", cls.lekki)
        cls.yaba_parent = cls.customer(cls.books, "YAB01", cls.yaba)
        cls.shared = cls.customer(cls.books, "ALL01", None)
        cls.ikeja_bursar = cls.person("finance.payment.create", branch=cls.ikeja)

    def lookup(self, user, code, branch=None, books=None):
        branch = branch or self.lekki
        return self.as_(user).get(self.url(
            f"held-receipts/customer-lookup/?for_branch={branch.pk}&code={code}", books))

    def test_ikeja_finds_lekkis_customer_by_code_and_learns_only_the_name(self):
        data = self.rows(self.lookup(self.ikeja_bursar, "ade01"))

        self.assertEqual(data, {"id": self.adeyemi.pk, "code": "ADE01",
                                "name": "Parent ADE01", "branch_id": self.lekki.pk})

    def test_a_customer_every_branch_shares_is_found_too(self):
        self.assertEqual(self.rows(self.lookup(self.ikeja_bursar, "ALL01"))["id"], self.shared.pk)

    def test_another_branchs_customer_is_not_found_at_lekki(self):
        response = self.lookup(self.ikeja_bursar, "YAB01")

        self.assertEqual(response.status_code, 404)

    def test_only_an_exact_code_answers(self):
        self.assertEqual(self.lookup(self.ikeja_bursar, "ADE").status_code, 404)
        self.assertEqual(self.lookup(self.ikeja_bursar, "").status_code, 400)

    def test_a_reader_without_the_receipt_key_is_refused(self):
        reader = self.person("finance.payment.view", branch=self.ikeja)

        self.assertEqual(self.lookup(reader, "ADE01").status_code, 403)

    def test_another_tenants_branch_is_unknown(self):
        response = self.lookup(self.ikeja_bursar, "ADE01", branch=self.rival_branch)

        self.assertEqual(response.status_code, 400)
        self.assertIn("No such branch", str(response.data))

    def test_a_one_branch_school_has_no_other_branch_to_hold_for(self):
        solo = self.person("finance.payment.create", tenant=self.solo_tenant)
        self.customer(self.solo_books, "SOLO1", self.solo_main)

        response = self.lookup(solo, "SOLO1", branch=self.solo_main, books=self.solo_books)

        self.assertEqual(response.status_code, 400)
        self.assertIn("only one branch", str(response.data))


# --------------------------------------------------------------------------- #
# 5. The close check in words                                                 #
# --------------------------------------------------------------------------- #

class CloseCheckWordingTests(_ReadsFixture):
    """Ikeja sends Lekki N1m of textbooks; then a broken journal books N50 on Ikeja's side only."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.january = FiscalPeriod.objects.get(entity=cls.books, period_no=1)
        book_goods_transfer(
            cls.books, from_branch=cls.ikeja, to_branch=cls.lekki, amount=1_000_000_00,
            transfer_date=JAN_15, inventory_account=Account.objects.get(entity=cls.books, code="1400"),
            purpose="Textbooks",
        )
        entry = JournalEntry.objects.create(
            entity=cls.books, branch=cls.ikeja, date=JAN_15, period=cls.january,
            source=JournalSource.SYSTEM,
        )
        JournalLine.objects.create(entry=entry, account=cls.ib, debit=5_000, credit=0,
                                   counterparty_branch=cls.lekki, line_no=1)
        JournalLine.objects.create(entry=entry, account=cls.audit_fee, debit=0, credit=5_000, line_no=2)
        post_journal(entry)

    def test_the_detail_names_the_branches_and_gives_naira(self):
        check = inter_branch_close_check(self.books, self.january)

        self.assertFalse(check.passed)
        self.assertEqual(check.detail, (
            "Ikeja Branch and Lekki Branch disagree: Ikeja Branch's books say Lekki Branch owes "
            "Ikeja Branch ₦1,000,050.00; Lekki Branch's books say Lekki Branch owes Ikeja Branch "
            "₦1,000,000.00; the inter-branch account nets to ₦50.00 debit, not zero"
        ))
        self.assertNotIn("kobo", check.detail)
        self.assertNotIn(str(self.ikeja.pk), check.detail)

    def test_a_branch_close_warns_in_the_same_words(self):
        check = inter_branch_close_check(self.books, self.january, branch=self.lekki)

        self.assertTrue(check.detail.startswith("Ikeja Branch and Lekki Branch disagree"))
        self.assertNotIn("nets to", check.detail)


# --------------------------------------------------------------------------- #
# 6. Re-saving a tax code or cost centre                                      #
# --------------------------------------------------------------------------- #

class MasterDataUpdateKeepsTests(_ReadsFixture):
    """Corona's bursar renames codes; what the request leaves out stays as it was."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = cls.person("finance.taxcode.create", "finance.costcenter.create")

    def post(self, path, body):
        return self.as_(self.admin).post(self.url(path), body, format="json")

    def test_an_update_without_a_treatment_keeps_the_codes_own(self):
        created = self.post("tax-codes/", {"code": "EXM", "name": "Exempt", "treatment": "EXEMPT"})
        renamed = self.post("tax-codes/", {"code": "EXM", "name": "Exempt supplies"})

        self.assertEqual((created.status_code, renamed.status_code), (201, 200), renamed.data)
        code = TaxCode.objects.get(entity=self.books, code="EXM")
        self.assertEqual((code.name, code.treatment, code.rate_bps), ("Exempt supplies", "EXEMPT", 0))

    def test_an_update_keeps_the_rate_and_accounts_it_does_not_name(self):
        self.post("tax-codes/", {"code": "VAT2", "rate_bps": 750, "collected_account": "2200",
                                 "is_recoverable": False})
        self.post("tax-codes/", {"code": "VAT2", "name": "VAT at 7.5%"})

        code = TaxCode.objects.get(entity=self.books, code="VAT2")
        self.assertEqual((code.rate_bps, code.treatment, code.collected_account.code, code.is_recoverable),
                         (750, "STANDARD", "2200", False))

    def test_a_create_without_a_treatment_is_standard(self):
        self.assertEqual(self.post("tax-codes/", {"code": "NEW", "rate_bps": 500}).status_code, 201)

        self.assertEqual(TaxCode.objects.get(entity=self.books, code="NEW").treatment, "STANDARD")

    def test_a_kept_rate_still_needs_a_standard_treatment(self):
        self.post("tax-codes/", {"code": "VAT3", "rate_bps": 750})

        refused = self.post("tax-codes/", {"code": "VAT3", "treatment": "EXEMPT"})
        moved = self.post("tax-codes/", {"code": "VAT3", "treatment": "EXEMPT", "rate_bps": 0})

        self.assertEqual(refused.status_code, 400)
        self.assertIn("rate_bps", str(refused.data))
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(TaxCode.objects.get(entity=self.books, code="VAT3").treatment, "EXEMPT")

    def test_a_cost_centre_keeps_its_parent_when_renamed(self):
        self.post("cost-centers/", {"code": "ADMIN", "name": "Administration"})
        self.post("cost-centers/", {"code": "FRONT", "name": "Front desk", "parent": "ADMIN"})
        self.post("cost-centers/", {"code": "FRONT", "name": "Reception"})

        front = CostCenter.objects.get(entity=self.books, code="FRONT")
        self.assertEqual((front.name, front.parent.code), ("Reception", "ADMIN"))
        self.post("cost-centers/", {"code": "FRONT", "parent": None})
        front.refresh_from_db()
        self.assertIsNone(front.parent)

    def test_a_branch_bound_bursar_still_cannot_write_a_shared_code(self):
        bursar = self.person("finance.taxcode.create", branch=self.ikeja)

        response = self.as_(bursar).post(self.url("tax-codes/"), {"code": "EXM2"}, format="json")

        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------- #
# 7. The fiscal calendar's gaps                                               #
# --------------------------------------------------------------------------- #

class RunwayGapTests(_ReadsFixture):
    """Corona's calendar covers January and February 2026, then nothing until January 2028."""

    def test_the_runway_names_today_and_every_uncovered_stretch(self):
        from .dashboard import _fiscal_runway

        year = FiscalYear.objects.create(
            entity=self.books, year=2028,
            start_date=datetime.date(2028, 1, 1), end_date=datetime.date(2028, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=self.books, fiscal_year=year, period_no=1, name="Jan 2028",
            start_date=datetime.date(2028, 1, 1), end_date=datetime.date(2028, 1, 31),
        )
        with patch("vs_finance.posting.tenant_today", return_value=JAN_15):
            runway = _fiscal_runway(self.books)

        self.assertEqual(runway["today"], "2026-01-15")
        self.assertEqual(runway["gaps"], [{"start": "2026-03-01", "end": "2027-12-31"}])

    def test_a_calendar_without_gaps_lists_none(self):
        from .dashboard import _fiscal_runway

        with patch("vs_finance.posting.tenant_today", return_value=JAN_15):
            self.assertEqual(_fiscal_runway(self.solo_books)["gaps"], [])


# --------------------------------------------------------------------------- #
# 8. A tax return's lines                                                     #
# --------------------------------------------------------------------------- #

class TaxReturnLinesTests(_ReadsFixture):
    """January's VAT: Ikeja bills 100k (7.5k VAT), Lekki bills 200k (15k VAT)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        vat = TaxCode.objects.get(entity=cls.books, code="VAT-STD")
        cls.ikeja_bill = cls.taxed(cls.books, cls.customer(cls.books, "IKJ1", cls.ikeja), cls.ikeja, 100_000, vat)
        cls.lekki_bill = cls.taxed(cls.books, cls.customer(cls.books, "LEK1", cls.lekki), cls.lekki, 200_000, vat)
        cls.obligation = TaxObligation.objects.get(entity=cls.books, code="VAT")
        cls.whole = cls.person("finance.tax.view")
        cls.lekki_reader = cls.person("finance.tax.view", branch=cls.lekki)
        cls.yaba_reader = cls.person("finance.tax.view", branch=cls.yaba)

    @classmethod
    def taxed(cls, books, customer, branch, amount, vat):
        invoice = Invoice.objects.create(entity=books, customer=customer, branch=branch,
                                         invoice_date=JAN_10, due_date=datetime.date(2026, 1, 25))
        InvoiceLine.objects.create(invoice=invoice, line_no=1, quantity=1, unit_price=amount,
                                   revenue_account=Account.objects.get(entity=books, code="4100"),
                                   tax_code=vat)
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def prepare(self, start=datetime.date(2026, 1, 1), end=JAN_31, obligation=None):
        from .tax_filing import prepare_filing

        return prepare_filing(obligation or self.obligation, period_start=start, period_end=end)

    def lines(self, user, filing, books=None):
        return self.rows(self.as_(user).get(self.url(f"tax-filings/{filing.pk}/lines/", books)))

    def summary(self, rows):
        return sorted((row["document"]["number"], row["branch_name"], row["role"], row["amount"],
                       row["is_late"]) for row in rows)

    def test_a_draft_lists_each_line_with_the_bill_behind_it(self):
        filing = self.prepare()

        rows = self.lines(self.whole, filing)

        self.assertEqual(self.summary(rows), sorted([
            (self.ikeja_bill.document_number, "Ikeja Branch", "PAYABLE", 7_500, False),
            (self.lekki_bill.document_number, "Lekki Branch", "PAYABLE", 15_000, False),
        ]))
        row = next(r for r in rows if r["branch_id"] == self.lekki.pk)
        self.assertEqual(row["document"], {"type": "INVOICE", "id": self.lekki_bill.pk,
                                           "number": self.lekki_bill.document_number})
        self.assertEqual((row["journal_id"], row["account"]["code"], row["date"]),
                         (self.lekki_bill.journal_id, "2200", JAN_10))
        self.assertEqual(sum(r["amount"] for r in rows), filing.gross_liability - filing.recoverable_amount)

    def test_a_branch_bound_reader_sees_only_their_branchs_lines(self):
        filing = self.prepare()

        rows = self.lines(self.lekki_reader, filing)
        yaba = self.as_(self.yaba_reader).get(self.url(f"tax-filings/{filing.pk}/lines/"))

        self.assertEqual([r["branch_id"] for r in rows], [self.lekki.pk])
        self.assertEqual(yaba.status_code, 404)

    def test_a_filed_return_lists_what_it_declared(self):
        from .tax_filing import file_filing

        filing = self.prepare()
        file_filing(filing, filed_date=JAN_31)
        later = self.taxed(self.books, self.customer(self.books, "LEK2", self.lekki), self.lekki, 40_000,
                           TaxCode.objects.get(entity=self.books, code="VAT-STD"))

        rows = self.lines(self.whole, filing)

        self.assertEqual(len(rows), 2)
        self.assertNotIn(later.document_number, [r["document"]["number"] for r in rows])

    def test_a_line_from_an_earlier_month_is_late(self):
        february = self.prepare(start=datetime.date(2026, 2, 1), end=datetime.date(2026, 2, 28))

        rows = self.lines(self.whole, february)

        self.assertEqual({r["is_late"] for r in rows}, {True})

    def test_a_reversed_bill_is_named_by_the_bill_on_both_lines(self):
        from .voids import void_invoice

        void_invoice(Invoice.objects.get(pk=self.ikeja_bill.pk), date=JAN_15)
        rows = self.lines(self.whole, self.prepare())

        ikeja = [r for r in rows if r["branch_id"] == self.ikeja.pk]
        self.assertEqual(sorted(r["amount"] for r in ikeja), [-7_500, 7_500])
        self.assertEqual({r["document"]["number"] for r in ikeja}, {self.ikeja_bill.document_number})

    def test_a_page_costs_the_same_however_many_lines_it_holds(self):
        reader = self.as_(self.whole)
        filing = self.prepare()
        two = self.queries(reader, self.url(f"tax-filings/{filing.pk}/lines/"))
        vat = TaxCode.objects.get(entity=self.books, code="VAT-STD")
        for code in ("LEK3", "LEK4"):
            self.taxed(self.books, self.customer(self.books, code, self.lekki), self.lekki, 10_000, vat)

        four = self.queries(reader, self.url(f"tax-filings/{self.prepare().pk}/lines/"))

        self.assertEqual(two, four)

    def test_a_reader_without_the_tax_key_is_refused(self):
        filing = self.prepare()
        reader = self.person("finance.journal.view")

        response = self.as_(reader).get(self.url(f"tax-filings/{filing.pk}/lines/"))

        self.assertEqual(response.status_code, 403)

    def test_another_tenants_return_is_not_found(self):
        rival_filing = self.prepare(obligation=TaxObligation.objects.get(entity=self.rival_books, code="VAT"))

        response = self.as_(self.whole).get(self.url(f"tax-filings/{rival_filing.pk}/lines/"))

        self.assertEqual(response.status_code, 404)

    def test_a_one_branch_school_counts_every_line_at_its_branch(self):
        vat = TaxCode.objects.get(entity=self.solo_books, code="VAT-STD")
        self.taxed(self.solo_books, self.customer(self.solo_books, "SOLO1", self.solo_main),
                   self.solo_main, 100_000, vat)
        filing = self.prepare(obligation=TaxObligation.objects.get(entity=self.solo_books, code="VAT"))
        reader = self.person("finance.tax.view", tenant=self.solo_tenant)

        rows = self.lines(reader, filing, books=self.solo_books)

        self.assertEqual([(r["branch_id"], r["amount"]) for r in rows], [(self.solo_main.pk, 7_500)])


# --------------------------------------------------------------------------- #
# 9. Who raised a journal, and whether they have left                         #
# --------------------------------------------------------------------------- #

class JournalMakerExitTests(_ReadsFixture):
    """Every journal row says whether its maker has left, read once for the page."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.reader = cls.person("finance.journal.view")

    def journal(self, maker):
        return JournalEntry.objects.create(entity=self.books, branch=self.ikeja, date=JAN_15,
                                           source=JournalSource.MANUAL, created_by=maker)

    def test_each_row_carries_the_flag_and_a_page_resolves_it_once(self):
        client = self.as_(self.reader)
        self.journal(self.person("finance.journal.view"))
        one = self.queries(client, self.url("journals/"))
        self.journal(self.person("finance.journal.view"))
        self.journal(None)

        three = self.queries(client, self.url("journals/"))
        rows = self.rows(client.get(self.url("journals/")))

        self.assertEqual(one, three)
        self.assertEqual(sorted(str(row["created_by_is_exited"]) for row in rows),
                         ["False", "False", "None"])


# --------------------------------------------------------------------------- #
# 10. Count tabs over archived years                                          #
# --------------------------------------------------------------------------- #

class ArchivedSummaryTests(_ReadsFixture):
    """FY2020 is archived: a paid 2020 bill and a 2020 journal are put away, an unpaid bill is not."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        year = FiscalYear.objects.create(
            entity=cls.books, year=2020, start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2020, 12, 31), status=PeriodStatus.CLOSED,
            archived_at=timezone.now(),
        )
        old_day = datetime.date(2020, 3, 1)
        JournalEntry.objects.create(entity=cls.books, branch=cls.ikeja, date=old_day,
                                    source=JournalSource.MANUAL, status=DocumentStatus.POSTED)
        JournalEntry.objects.create(entity=cls.books, branch=cls.ikeja, date=JAN_15,
                                    source=JournalSource.MANUAL, status=DocumentStatus.POSTED)
        parent = cls.customer(cls.books, "OLD1", cls.ikeja)
        for payment_status in (InvoicePaymentStatus.PAID, InvoicePaymentStatus.UNPAID):
            invoice = Invoice.objects.create(entity=cls.books, customer=parent, branch=cls.ikeja,
                                             invoice_date=old_day, due_date=old_day)
            Invoice.objects.filter(pk=invoice.pk).update(status=DocumentStatus.POSTED,
                                                         payment_status=payment_status)
        cls.year = year
        cls.reader = cls.person("finance.journal.view", "finance.invoice.view")

    def counts(self, path):
        client = self.as_(self.reader)
        listed = client.get(self.url(path))
        summary = self.rows(client.get(self.url(path + "summary/")))
        return listed.data["pagination"]["totalItems"], summary

    def test_the_journal_tabs_count_what_the_list_shows(self):
        listed, summary = self.counts("journals/")
        everything = self.rows(self.as_(self.reader).get(self.url("journals/summary/?include_archived=true")))

        self.assertEqual((listed, summary["total"]), (1, 1))
        self.assertEqual(everything["total"], 2)

    def test_the_invoice_tabs_keep_a_bill_still_owed_and_put_a_paid_one_away(self):
        listed, summary = self.counts("invoices/")
        everything = self.rows(self.as_(self.reader).get(self.url("invoices/summary/?include_archived=true")))

        self.assertEqual((listed, summary["by_status"]["total"]), (1, 1))
        self.assertEqual(summary["by_status"]["paid"], 0)
        self.assertEqual((everything["by_status"]["total"], everything["by_status"]["paid"]), (2, 1))


# --------------------------------------------------------------------------- #
# 11. Where each branch stands in each period and year                        #
# --------------------------------------------------------------------------- #

class CalendarBranchStatesTests(_ReadsFixture):
    """Lekki has closed January on its own; February is closed for the whole school."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.january = FiscalPeriod.objects.get(entity=cls.books, period_no=1)
        cls.closed_at = timezone.now()
        BranchFiscalPeriod.objects.create(period=cls.january, branch=cls.lekki,
                                          status=PeriodStatus.CLOSED, closed_at=cls.closed_at)
        cls.whole = cls.person("finance.journal.view")
        cls.ikeja_reader = cls.person("finance.journal.view", branch=cls.ikeja)

    def periods(self, user, query="all=true&year=2026&include_branches=true"):
        return self.rows(self.as_(user).get(self.url(f"periods/?{query}")))

    def states(self, row):
        return [(s["branch_name"], s["status"]) for s in row["branch_states"]]

    def test_every_branch_in_reach_is_listed_against_each_period(self):
        january, february = self.periods(self.whole)

        self.assertEqual(self.states(january), [
            ("Ikeja Branch", "OPEN"), ("Lekki Branch", "CLOSED"), ("Yaba Branch", "OPEN"),
        ])
        self.assertEqual(january["branch_states"][1]["branch"], self.lekki.pk)
        self.assertIsNotNone(january["branch_states"][1]["closed_at"])
        self.assertEqual({status for _name, status in self.states(february)}, {"CLOSED"})

    def test_a_branch_bound_reader_sees_only_their_branch(self):
        january, _february = self.periods(self.ikeja_reader)

        self.assertEqual(self.states(january), [("Ikeja Branch", "OPEN")])

    def test_without_the_flag_the_rows_are_as_before(self):
        rows = self.periods(self.whole, query="all=true&year=2026")

        self.assertNotIn("branch_states", rows[0])

    def test_the_paginated_list_and_the_years_carry_them_too(self):
        paged = self.periods(self.whole, query="include_branches=true")
        years = self.rows(self.as_(self.whole).get(self.url("fiscal-years/?include_branches=true")))

        self.assertEqual(len(paged[0]["branch_states"]), 3)
        self.assertEqual(self.states(years[0]), [
            ("Ikeja Branch", "OPEN"), ("Lekki Branch", "OPEN"), ("Yaba Branch", "OPEN"),
        ])

    def test_the_states_cost_two_queries_whatever_the_page_holds(self):
        client = self.as_(self.whole)
        before = self.queries(client, self.url("periods/?all=true&year=2026&include_branches=true"))
        plain = self.queries(client, self.url("periods/?all=true&year=2026"))
        FiscalPeriod.objects.create(
            entity=self.books, fiscal_year=self.january.fiscal_year, period_no=3, name="Mar 2026",
            start_date=datetime.date(2026, 3, 1), end_date=datetime.date(2026, 3, 31),
        )

        after = self.queries(client, self.url("periods/?all=true&year=2026&include_branches=true"))

        self.assertEqual((before - plain, after), (2, before))

    def test_a_one_branch_school_lists_its_only_branch(self):
        reader = self.person("finance.journal.view", tenant=self.solo_tenant)

        (january,) = self.rows(self.as_(reader).get(
            self.url("periods/?all=true&year=2026&include_branches=true", self.solo_books)))

        self.assertEqual(self.states(january), [("Main Branch", "OPEN")])
