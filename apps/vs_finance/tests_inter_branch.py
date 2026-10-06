"""Inter-branch transfers: each branch books its own side, and the pair balance says who owes whom.

Corona runs Ikeja, Lekki and Yaba. Lekki is short for diesel and asks Ikeja for
1m; Ikeja sends it; Lekki confirms it arrived. Mrs Adeyemi pays 400k of her son's
Lekki fees into Ikeja's account, and Ikeja forwards it. Ikeja pays the 3m audit
fee and recharges it by pupil numbers. Tunde moves from Ikeja to Lekki owing
170k, and his whole position moves with him: his debit note, his unapplied
credit and the part of his term not yet taught. When Lekki later cancels the
term, January, which Ikeja earned and kept, is taken back from Ikeja rather than
out of Lekki's books, and a credit on his textbook bill takes Ikeja's revenue
and VAT back the same way; a write-off stays Lekki's loss. Ikeja's central
store sends Lekki textbooks.

Every one of those posts two journals, one per branch, so no entry ever mixes
branches, and the inter-branch account nets to zero across the school. Each
reader sees a transfer only when they work in one of its branches, and a pair
balance only when their branch is part of it. Single Site has one branch, so
there is nobody to transfer to and nothing books.
"""
from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient

from .banking import _unmatched_gl_lines, import_statement_lines, match_line
from .branch_ledger import ledger_lines
from .constants import (
    CreditNoteKind,
    DocumentStatus,
    InterBranchLegRole,
    InterBranchTransferKind,
    JournalSource,
    PeriodStatus,
    RechargeBasis,
    SharedCostTreatment,
)
from .control_accounts import ControlAccountLockedError
from .exceptions import InterBranchError, InterBranchUnavailableError, PeriodClosedError, PostingError
from .inter_branch import (
    book_goods_transfer,
    pair_balances,
    run_recharge,
    split_by_weight,
    transfer_open_receivables,
    void_inter_branch_transfer,
    void_recharge,
)
from .models import (
    Account,
    BankAccount,
    CreditNote,
    CreditNoteLine,
    FinanceAuditLog,
    FiscalPeriod,
    FiscalYear,
    HeldForBranchReceipt,
    InterBranchTransfer,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    Payment,
    SharedCostRule,
    SharedCostRuleShare,
)
from .posting import journal_reversal_action, post_journal, resolve_period
from .receivables import post_invoice, post_payment
from .tests_branch_scope import _FinanceBranchFixture
from .voids import void_invoice, void_payment

JAN_15 = datetime.date(2026, 1, 15)
JAN_20 = datetime.date(2026, 1, 20)
_seq = itertools.count(1)

TRANSFER_KEYS = (
    "finance.interbranch.view", "finance.interbranch.request", "finance.interbranch.transfer",
    "finance.interbranch.confirm", "finance.interbranch.reverse", "finance.interbranch.recharge",
    "finance.payment.view", "finance.payment.create", "finance.payment.reverse",
)


def net(account, branch=None, counterparty=None):
    """Debits less credits on ``account``, in one branch's books and against one counterparty."""
    lines = ledger_lines().filter(account=account)
    if branch is not None:
        lines = lines.filter(entry__branch=branch)
    if counterparty is not None:
        lines = lines.filter(counterparty_branch=counterparty)
    return sum(line.debit - line.credit for line in lines)


class _InterBranchFixture(_FinanceBranchFixture):
    """Corona's three branches, each with its own bank account, and Single Site's one."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_bank = cls.bank(cls.books, "1151", "Ikeja GTBank", cls.ikeja)
        cls.lekki_bank = cls.bank(cls.books, "1152", "Lekki Access", cls.lekki, collection=True)
        cls.yaba_bank = cls.bank(cls.books, "1153", "Yaba Zenith", cls.yaba, collection=True)
        cls.solo_bank = cls.bank(cls.solo_books, "1151", "Main GTBank", cls.solo_main)
        cls.ib = Account.objects.get(entity=cls.books, code="1260")
        cls.held = Account.objects.get(entity=cls.books, code="2190")
        cls.ar = Account.objects.get(entity=cls.books, code="1200")
        cls.audit_fee = Account.objects.get(entity=cls.books, code="5300")
        FiscalPeriod.objects.create(
            entity=cls.books, fiscal_year=FiscalYear.objects.get(entity=cls.books),
            period_no=2, name="Feb 2026", start_date=datetime.date(2026, 2, 1),
            end_date=datetime.date(2026, 2, 28), status=PeriodStatus.CLOSED,
        )

    @classmethod
    def bank(cls, books, code, name, branch, *, collection=False):
        ledger = Account.objects.create(
            entity=books, code=code, name=name,
            account_type=Account.objects.get(entity=books, code="1100").account_type,
            is_postable=True,
        )
        return BankAccount.objects.create(
            entity=books, name=name, branch=branch, gl_account=ledger,
            is_primary_collection=collection,
        )

    @classmethod
    def bursar(cls, *branches, tenant=None, keys=TRANSFER_KEYS):
        tenant = tenant or cls.tenant
        user = cls.user_for(tenant, f"ib-{next(_seq)}@corona.test")
        for branch in branches:
            cls.grant(user, *keys, tenant=tenant, role_key=f"ib-{next(_seq)}", branch=branch)
        return user

    def client_for(self, *branches, **kwargs):
        return TenantAPIClient(user=self.bursar(*branches, **kwargs))

    def url(self, path="", books=None):
        books = books or self.books
        sep = "&" if "?" in path else "?"
        return f"/v1/finance/{path}{sep}entity={books.code}"

    def send(self, client, *, source, to_branch, amount=1_000_000_00, **extra):
        return client.post(self.url("inter-branch-transfers/"), {
            "from_bank_account": source.pk, "to_branch": to_branch.pk, "amount": amount,
            "transfer_date": "2026-01-15", "purpose": "Diesel for the generators", **extra,
        }, format="json")

    @classmethod
    def posted_invoice(cls, customer, branch, amount=100_000):
        invoice = Invoice.objects.create(
            entity=cls.books, customer=customer, branch=branch,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=amount,
            revenue_account=Account.objects.get(entity=cls.books, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice


class SplitByWeightTests(SimpleTestCase):
    def test_shares_add_up_exactly_and_the_odd_kobo_go_to_the_largest_fractions(self):
        shares = split_by_weight(100, {1: 1, 2: 1, 3: 1})
        self.assertEqual(sum(shares.values()), 100)
        self.assertEqual(shares, {1: 34, 2: 33, 3: 33})

    def test_pupil_counts_split_the_audit_fee(self):
        self.assertEqual(
            split_by_weight(3_000_000_00, {1: 500, 2: 300, 3: 200}),
            {1: 1_500_000_00, 2: 900_000_00, 3: 600_000_00},
        )


class CashTransferTests(_InterBranchFixture):
    """Ikeja lends Lekki 1m for diesel."""

    def test_sending_posts_one_journal_per_branch_and_lekki_owes_ikeja(self):
        response = self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)

        self.assertEqual(response.status_code, 201, response.data)
        transfer = InterBranchTransfer.objects.get()
        self.assertEqual((transfer.status, transfer.branch_id, transfer.to_branch_id),
                         (DocumentStatus.POSTED, self.ikeja.pk, self.lekki.pk))
        self.assertEqual(transfer.to_bank_account, self.lekki_bank)
        legs = {leg.role: leg.journal for leg in transfer.legs.all()}
        sending, receiving = legs[InterBranchLegRole.SENDING], legs[InterBranchLegRole.RECEIVING]
        self.assertEqual((sending.branch_id, receiving.branch_id), (self.ikeja.pk, self.lekki.pk))
        self.assertEqual({sending.reference, receiving.reference}, {transfer.document_number})
        self.assertEqual(sending.source, JournalSource.BANK)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), 1_000_000_00)
        self.assertEqual(net(self.ikeja_bank.gl_account, self.ikeja), -1_000_000_00)
        self.assertEqual(net(self.lekki_bank.gl_account, self.lekki), 1_000_000_00)
        self.assertEqual(net(self.ib, self.lekki, self.ikeja), -1_000_000_00)
        self.assertEqual(net(self.ib), 0)
        for entry in (sending, receiving):
            debit, credit = entry.totals()
            self.assertEqual(debit, credit)
        grid = pair_balances(self.books)
        self.assertEqual(grid["net_total"], 0)
        self.assertEqual(len(grid["pairs"]), 1)
        pair = grid["pairs"][0]
        self.assertEqual((pair["owed_to"]["id"], pair["owed_by"]["id"], pair["amount"], pair["balanced"]),
                         (self.ikeja.pk, self.lekki.pk, 1_000_000_00, True))
        self.assertEqual(journal_reversal_action(sending)["document_type"], "INTER_BRANCH_TRANSFER")

    def test_each_bank_side_reaches_its_own_reconciliation(self):
        self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)
        transfer = InterBranchTransfer.objects.get()

        sent = JournalLine.objects.get(entry__inter_branch_leg__transfer=transfer,
                                       account=self.ikeja_bank.gl_account)
        received = JournalLine.objects.get(entry__inter_branch_leg__transfer=transfer,
                                           account=self.lekki_bank.gl_account)
        self.assertEqual([l.pk for l in _unmatched_gl_lines(self.ikeja_bank)], [sent.pk])
        self.assertEqual([l.pk for l in _unmatched_gl_lines(self.lekki_bank)], [received.pk])

    def test_lekki_requests_ikeja_sends_and_lekki_confirms(self):
        lekki, ikeja = self.client_for(self.lekki), self.client_for(self.ikeja)
        asked = lekki.post(self.url("inter-branch-transfers/requests/"), {
            "from_branch": self.ikeja.pk, "amount": 1_000_000_00, "purpose": "Diesel",
            "transfer_date": "2026-01-15", "repay_by": "2026-03-31",
        }, format="json")
        self.assertEqual(asked.status_code, 201, asked.data)
        pk = asked.data["data"]["id"]
        self.assertEqual(asked.data["data"]["stage"], "REQUESTED")
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

        lekki_sends = lekki.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                                 {"from_bank_account": self.lekki_bank.pk}, format="json")
        self.assertEqual(lekki_sends.status_code, 403, lekki_sends.data)

        sent = ikeja.post(self.url(f"inter-branch-transfers/{pk}/send/"),
                          {"from_bank_account": self.ikeja_bank.pk}, format="json")
        self.assertEqual(sent.status_code, 201, sent.data)
        self.assertEqual(sent.data["data"]["stage"], "SENT")
        self.assertEqual(sent.data["data"]["repay_by"], "2026-03-31")

        ikeja_confirms = ikeja.post(self.url(f"inter-branch-transfers/{pk}/confirm/"), {}, format="json")
        self.assertEqual(ikeja_confirms.status_code, 403, ikeja_confirms.data)
        confirmed = lekki.post(self.url(f"inter-branch-transfers/{pk}/confirm/"),
                               {"arrival_date": "2026-01-16"}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["data"]["stage"], "RECEIVED")
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), 1_000_000_00)
        actions = set(FinanceAuditLog.objects.filter(target_type="InterBranchTransfer", branch=self.lekki)
                      .values_list("action", flat=True))
        self.assertEqual(actions, {"INTER_BRANCH_REQUESTED", "INTER_BRANCH_SENT", "INTER_BRANCH_CONFIRMED"})

    def test_ikeja_declines_a_request_and_nothing_is_booked(self):
        asked = self.client_for(self.lekki).post(self.url("inter-branch-transfers/requests/"), {
            "from_branch": self.ikeja.pk, "amount": 500_000_00, "purpose": "Diesel",
        }, format="json")
        pk = asked.data["data"]["id"]

        declined = self.client_for(self.ikeja).post(
            self.url(f"inter-branch-transfers/{pk}/decline/"), {"reason": "Short ourselves"}, format="json")

        self.assertEqual(declined.status_code, 200, declined.data)
        self.assertEqual(declined.data["data"]["stage"], "DECLINED")
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

    def test_a_repayment_the_other_way_reduces_what_lekki_owes(self):
        self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)
        repaid = self.send(self.client_for(self.lekki), source=self.lekki_bank, to_branch=self.ikeja,
                           amount=400_000_00, purpose="Part repayment",
                           to_bank_account=self.ikeja_bank.pk)

        self.assertEqual(repaid.status_code, 201, repaid.data)
        pair = pair_balances(self.books)["pairs"][0]
        self.assertEqual((pair["owed_by"]["id"], pair["amount"]), (self.lekki.pk, 600_000_00))

    def test_both_branches_must_be_open_on_the_date(self):
        response = self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki,
                             transfer_date="2026-02-10")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(JournalEntry.objects.filter(entity=self.books).exists())

    def test_money_cannot_be_sent_from_another_branchs_account(self):
        ikeja_and_lekki = self.client_for(self.ikeja, self.lekki)
        to_itself = self.send(ikeja_and_lekki, source=self.lekki_bank, to_branch=self.lekki)
        wrong_receiver = self.send(ikeja_and_lekki, source=self.ikeja_bank, to_branch=self.lekki,
                                   to_bank_account=self.yaba_bank.pk)
        not_reached = self.send(self.client_for(self.yaba), source=self.ikeja_bank, to_branch=self.lekki)

        self.assertEqual(to_itself.status_code, 400, to_itself.data)
        self.assertEqual(wrong_receiver.status_code, 400, wrong_receiver.data)
        self.assertEqual(not_reached.status_code, 404, not_reached.data)
        self.assertFalse(InterBranchTransfer.objects.exists())

    def test_void_reverses_both_sides_until_a_statement_line_is_matched(self):
        client = self.client_for(None)
        first = self.send(client, source=self.ikeja_bank, to_branch=self.lekki)
        second = self.send(client, source=self.ikeja_bank, to_branch=self.lekki, amount=50_000_00)
        matched = InterBranchTransfer.objects.get(pk=second.data["data"]["id"])
        statement_line = import_statement_lines(self.lekki_bank, [
            {"txn_date": JAN_15, "amount": 50_000_00},
        ])[1][0]
        match_line(statement_line, JournalLine.objects.get(
            entry__inter_branch_leg__transfer=matched, account=self.lekki_bank.gl_account))

        voided = client.post(self.url(f"inter-branch-transfers/{first.data['data']['id']}/void/"),
                             {"date": "2026-01-20"}, format="json")
        refused = client.post(self.url(f"inter-branch-transfers/{matched.pk}/void/"),
                              {"date": "2026-01-20"}, format="json")

        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["data"]["stage"], "VOIDED")
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), 50_000_00)
        self.assertEqual(net(self.ib), 0)
        reversals = JournalEntry.objects.filter(reverses__inter_branch_leg__transfer_id=first.data["data"]["id"])
        self.assertEqual(sorted(reversals.values_list("branch_id", flat=True)),
                         sorted([self.ikeja.pk, self.lekki.pk]))

    def test_voiding_needs_somebody_who_works_in_both_branches(self):
        sent = self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)

        refused = self.client_for(self.ikeja).post(
            self.url(f"inter-branch-transfers/{sent.data['data']['id']}/void/"), {}, format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(InterBranchTransfer.objects.get().status, DocumentStatus.POSTED)

    def test_one_side_cannot_be_reversed_on_its_own(self):
        from .posting import reverse_journal

        self.send(self.client_for(self.ikeja), source=self.ikeja_bank, to_branch=self.lekki)
        journal = JournalEntry.objects.get(branch=self.lekki)

        with self.assertRaisesMessage(PostingError, "inter-branch-transfers"):
            reverse_journal(journal)

    def test_the_approval_route_sends_it_once_approved(self):
        from vs_workflow.handlers.registry import get_handler

        transfer = InterBranchTransfer.objects.create(
            entity=self.books, kind=InterBranchTransferKind.CASH, branch=self.ikeja,
            to_branch=self.lekki, amount=1_000, transfer_date=JAN_15, purpose="Diesel",
            from_bank_account=self.ikeja_bank, to_bank_account=self.lekki_bank,
        )
        handler = get_handler("finance.inter_branch_transfer")

        handler.validate_document(transfer, None)
        handler.post(transfer, actor_user=None)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, DocumentStatus.POSTED)
        self.assertEqual(transfer.legs.count(), 2)

    def test_a_typed_journal_cannot_touch_the_inter_branch_account(self):
        entry = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=JAN_15, period=resolve_period(self.books, JAN_15),
            source=JournalSource.MANUAL,
        )
        JournalLine.objects.create(entry=entry, account=self.ib, debit=1_000, credit=0,
                                   counterparty_branch=self.lekki, line_no=1)
        JournalLine.objects.create(entry=entry, account=self.audit_fee, debit=0, credit=1_000, line_no=2)

        with self.assertRaisesMessage(ControlAccountLockedError, "inter-branch transfer"):
            post_journal(entry)

    def test_a_line_cannot_name_its_own_branch_as_the_other_side(self):
        entry = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=JAN_15, period=resolve_period(self.books, JAN_15),
            source=JournalSource.SYSTEM,
        )
        JournalLine.objects.create(entry=entry, account=self.ib, debit=1_000, credit=0,
                                   counterparty_branch=self.ikeja, line_no=1)
        JournalLine.objects.create(entry=entry, account=self.audit_fee, debit=0, credit=1_000, line_no=2)

        with self.assertRaisesMessage(PostingError, "its own branch"):
            post_journal(entry)

    def test_bank_transfer_between_branches_points_here(self):
        response = self.client_for(None, keys=("finance.banktransfer.create",)).post(
            self.url("bank-transfers/"), {
                "from_account": self.ikeja_bank.pk, "to_account": self.lekki_bank.pk,
                "amount": 1_000, "transfer_date": "2026-01-15", "narration": "Lend",
            }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("/finance/inter-branch-transfers/", str(response.data))


class VisibilityTests(_InterBranchFixture):
    """Ikeja lends Lekki 1m; Lekki lends Yaba 200k."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ikeja_to_lekki = book_goods_transfer(
            cls.books, from_branch=cls.ikeja, to_branch=cls.lekki, amount=1_000_000_00,
            transfer_date=JAN_15, inventory_account=Account.objects.get(entity=cls.books, code="1400"),
            purpose="Textbooks",
        )
        cls.lekki_to_yaba = book_goods_transfer(
            cls.books, from_branch=cls.lekki, to_branch=cls.yaba, amount=200_000_00,
            transfer_date=JAN_15, inventory_account=Account.objects.get(entity=cls.books, code="1400"),
            purpose="Chairs",
        )

    def listed(self, client):
        return {row["id"] for row in client.get(self.url("inter-branch-transfers/")).data["data"]}

    def test_a_transfer_is_seen_from_either_branch_and_not_from_a_third(self):
        self.assertEqual(self.listed(self.client_for(self.ikeja)), {self.ikeja_to_lekki.pk})
        self.assertEqual(self.listed(self.client_for(self.lekki)),
                         {self.ikeja_to_lekki.pk, self.lekki_to_yaba.pk})
        self.assertEqual(self.listed(self.client_for(self.yaba)), {self.lekki_to_yaba.pk})
        self.assertEqual(self.listed(self.client_for(None)),
                         {self.ikeja_to_lekki.pk, self.lekki_to_yaba.pk})
        detail = self.client_for(self.yaba).get(self.url(f"inter-branch-transfers/{self.ikeja_to_lekki.pk}/"))
        self.assertEqual(detail.status_code, 404, detail.data)

    def test_a_branch_reader_sees_only_the_pairs_their_branch_is_part_of(self):
        ikeja = self.client_for(self.ikeja).get(self.url("inter-branch-balances/")).data["data"]
        whole = self.client_for(None).get(self.url("inter-branch-balances/")).data["data"]

        self.assertEqual([(p["owed_to"]["id"], p["owed_by"]["id"], p["amount"]) for p in ikeja["pairs"]],
                         [(self.ikeja.pk, self.lekki.pk, 1_000_000_00)])
        self.assertIsNone(ikeja["net_total"])
        self.assertEqual(len(whole["pairs"]), 2)
        self.assertEqual(whole["net_total"], 0)

    def test_a_reader_without_the_view_key_is_refused(self):
        response = self.client_for(None, keys=("finance.journal.view",)).get(self.url("inter-branch-transfers/"))

        self.assertEqual(response.status_code, 403, response.data)

    def test_another_tenant_cannot_open_these_books(self):
        rival = self.client_for(None, tenant=self.rival_tenant)

        self.assertEqual(rival.get(self.url("inter-branch-transfers/")).status_code, 404)
        self.assertEqual(rival.get(self.url("inter-branch-balances/")).status_code, 404)

    def test_goods_are_undone_by_sending_them_back_not_by_a_void(self):
        with self.assertRaisesMessage(InterBranchError, "Send them back"):
            void_inter_branch_transfer(self.ikeja_to_lekki)


class ForwardedReceiptTests(_InterBranchFixture):
    """Mrs Adeyemi pays 400k of her son's Lekki fees into Ikeja's account."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.adeyemi = cls.customer(cls.books, "ADEYEMI", cls.lekki)
        cls.lekki_invoice = cls.posted_invoice(cls.adeyemi, cls.lekki, amount=400_000_00)

    def forward(self, client, held, **extra):
        return client.post(self.url(f"held-receipts/{held}/forward/"),
                           {"transfer_date": "2026-01-16", **extra}, format="json")

    def hold(self, client, **extra):
        return client.post(self.url("held-receipts/"), {
            "bank_account": self.ikeja_bank.pk, "for_branch": self.lekki.pk,
            "customer": "ADEYEMI", "amount": 400_000_00, "receipt_date": "2026-01-15", **extra,
        }, format="json")

    def test_ikeja_holds_it_for_lekki_and_forwarding_pays_the_lekki_invoice(self):
        ikeja = self.client_for(self.ikeja)
        held = self.hold(ikeja)
        self.assertEqual(held.status_code, 201, held.data)
        self.assertEqual(net(self.held, self.ikeja, self.lekki), -400_000_00)
        self.assertEqual(net(self.ikeja_bank.gl_account), 400_000_00)
        lekki_sees = self.client_for(self.lekki).get(self.url("held-receipts/")).data["data"]
        self.assertEqual([row["id"] for row in lekki_sees], [held.data["data"]["id"]])

        forwarded = self.forward(ikeja, held.data["data"]["id"])

        self.assertEqual(forwarded.status_code, 201, forwarded.data)
        transfer = InterBranchTransfer.objects.get()
        self.assertEqual(transfer.kind, InterBranchTransferKind.FORWARDED_RECEIPT)
        receipt = transfer.receipt
        self.assertEqual((receipt.branch_id, receipt.status, receipt.deposit_account_id),
                         (self.lekki.pk, DocumentStatus.POSTED, self.lekki_bank.gl_account_id))
        self.lekki_invoice.refresh_from_db()
        self.assertEqual(self.lekki_invoice.balance_due, 0)
        self.assertEqual(net(self.held), 0)
        self.assertEqual(net(self.ikeja_bank.gl_account), 0)
        self.assertEqual(net(self.lekki_bank.gl_account, self.lekki), 400_000_00)
        self.assertEqual(net(self.ib), 0)
        self.assertEqual(pair_balances(self.books)["pairs"], [])

    def test_ikeja_cannot_apply_it_to_another_branchs_customer(self):
        yaba_parent = self.customer(self.books, "YABAPARENT", self.yaba)

        response = self.hold(self.client_for(self.ikeja), customer=yaba_parent.code)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(HeldForBranchReceipt.objects.exists())

    def test_an_account_not_yet_given_a_branch_holds_nothing(self):
        shared = self.bank(self.books, "1154", "School UBA", None)

        response = self.hold(self.client_for(None), bank_account=shared.pk)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("has not been given a branch", str(response.data))
        self.assertFalse(HeldForBranchReceipt.objects.exists())

    def test_voiding_the_forward_unpays_the_invoice_and_frees_the_money_to_forward_again(self):
        ikeja = self.client_for(self.ikeja)
        held = self.hold(ikeja).data["data"]["id"]
        self.forward(ikeja, held)
        transfer = InterBranchTransfer.objects.get()

        with self.assertRaisesMessage(PostingError, "void that transfer instead"):
            void_payment(transfer.receipt)
        void_inter_branch_transfer(transfer, date=JAN_20)

        transfer.receipt.refresh_from_db()
        self.lekki_invoice.refresh_from_db()
        self.assertEqual(transfer.receipt.status, DocumentStatus.REVERSED)
        self.assertEqual(self.lekki_invoice.balance_due, 400_000_00)
        self.assertEqual(net(self.held, self.ikeja, self.lekki), -400_000_00)
        again = self.forward(ikeja, held)
        self.assertEqual(again.status_code, 201, again.data)

    def test_a_held_receipt_forwarded_cannot_be_voided_until_the_forward_is(self):
        ikeja = self.client_for(self.ikeja)
        held = self.hold(ikeja).data["data"]["id"]
        self.forward(ikeja, held)

        refused = ikeja.post(self.url(f"held-receipts/{held}/void/"), {}, format="json")

        self.assertEqual(refused.status_code, 400, refused.data)


class RechargeTests(_InterBranchFixture):
    """Ikeja pays the 3m audit fee and recharges it by pupil numbers."""

    def recharge(self, **extra):
        values = {
            "paying_branch": self.ikeja, "expense_account": self.audit_fee, "amount": 3_000_000_00,
            "recharge_date": JAN_15, "narration": "Audit fee",
            "basis": RechargeBasis.COUNTS,
            "weights": {self.ikeja.pk: 500, self.lekki.pk: 300, self.yaba.pk: 200},
        }
        values.update(extra)
        return run_recharge(self.books, **values)

    def test_counts_split_the_cost_into_inter_branch_balances(self):
        recharge = self.recharge()

        self.assertEqual(recharge.transfers.count(), 2)
        self.assertEqual(net(self.audit_fee, self.ikeja), -1_500_000_00)
        self.assertEqual(net(self.audit_fee, self.lekki), 900_000_00)
        self.assertEqual(net(self.audit_fee, self.yaba), 600_000_00)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), 900_000_00)
        self.assertEqual(net(self.ib, self.ikeja, self.yaba), 600_000_00)
        self.assertEqual(net(self.ib), 0)

    def test_fixed_percentages_from_the_rule_and_an_absorbed_cost_is_refused(self):
        rule = SharedCostRule.objects.create(
            entity=self.books, name="Insurance", treatment=SharedCostTreatment.RECHARGE,
            basis=RechargeBasis.PERCENTAGES, expense_account=self.audit_fee,
        )
        for branch, bps in ((self.ikeja, 3000), (self.lekki, 3000), (self.yaba, 4000)):
            SharedCostRuleShare.objects.create(rule=rule, branch=branch, percent_bps=bps)
        absorbed = SharedCostRule.objects.create(entity=self.books, name="Audit fee")

        recharge = self.recharge(rule=rule, basis=None, weights=None, expense_account=None,
                                 amount=1_000_000_00)

        self.assertEqual(net(self.audit_fee, self.yaba), 400_000_00)
        self.assertEqual(recharge.basis, RechargeBasis.PERCENTAGES)
        with self.assertRaisesMessage(InterBranchError, "has the paying branch absorb it"):
            self.recharge(rule=absorbed)
        with self.assertRaisesMessage(InterBranchError, "total 100"):
            self.recharge(basis=RechargeBasis.PERCENTAGES,
                          weights={self.ikeja.pk: 5000, self.lekki.pk: 4000})

    def test_a_share_is_voided_only_with_its_recharge(self):
        recharge = self.recharge()

        with self.assertRaisesMessage(InterBranchError, "void the recharge"):
            void_inter_branch_transfer(recharge.transfers.first())
        void_recharge(recharge, date=JAN_20)

        self.assertEqual(net(self.audit_fee, self.lekki), 0)
        self.assertEqual(net(self.ib), 0)
        self.assertFalse(recharge.transfers.exclude(status=DocumentStatus.REVERSED).exists())

    def test_the_api_shows_a_lekki_reader_only_lekkis_share(self):
        self.recharge()

        rows = self.client_for(self.lekki).get(self.url("recharges/")).data["data"]
        refused = self.client_for(self.lekki, keys=("finance.interbranch.view",)).post(
            self.url("recharges/"), {"amount": 1}, format="json")

        self.assertEqual([line["branch_id"] for line in rows[0]["lines"]], [self.lekki.pk])
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_a_branch_bound_caller_cannot_change_the_shared_cost_rules(self):
        refused = self.client_for(self.ikeja).post(
            self.url("shared-cost-rules/"), {"name": "Audit fee", "treatment": "RECHARGE"}, format="json")
        saved = self.client_for(None).post(
            self.url("shared-cost-rules/"),
            {"name": "Audit fee", "treatment": "RECHARGE", "basis": "PERCENTAGES",
             "shares": [{"branch": self.ikeja.pk, "percent": 60}, {"branch": self.lekki.pk, "percent": 40}]},
            format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(saved.status_code, 201, saved.data)
        self.assertEqual([s["percent"] for s in saved.data["data"]["shares"]], [60, 40])


class ReceivableMoveTests(_InterBranchFixture):
    """Tunde moves from Ikeja to Lekki owing 170k, and his debt moves with him."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.first = cls.posted_invoice(cls.tunde, cls.ikeja)
        cls.second = cls.posted_invoice(cls.tunde, cls.ikeja)
        cls.paid = cls.posted_invoice(cls.tunde, cls.ikeja)
        receipt = Payment.objects.create(
            entity=cls.books, branch=cls.ikeja, customer=cls.tunde, payment_date=JAN_15,
            amount=130_000, deposit_account=cls.ikeja_bank.gl_account,
        )
        post_payment(receipt, allocations=[(cls.paid, 100_000), (cls.second, 30_000)])

    def move(self, **kwargs):
        return transfer_open_receivables(self.tunde, self.ikeja, self.lekki, None, move_date=JAN_15, **kwargs)

    def test_lekki_takes_over_the_receivable_and_owes_ikeja(self):
        moved = self.move()

        self.assertEqual((moved.amount, moved.invoice_count), (170_000, 2))
        self.assertEqual(set(moved.invoice_ids), {self.first.pk, self.second.pk})
        for invoice in (self.first, self.second):
            invoice.refresh_from_db()
            self.assertEqual(invoice.branch_id, self.lekki.pk)
            self.assertEqual(invoice.journal.branch_id, self.ikeja.pk)
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.branch_id, self.ikeja.pk)
        self.assertEqual(net(self.ar, self.ikeja), 0)
        self.assertEqual(net(self.ar, self.lekki), 170_000)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), 170_000)
        self.assertEqual(net(self.ib), 0)
        audited = FinanceAuditLog.objects.filter(action="RECEIVABLE_TRANSFERRED")
        self.assertEqual(set(audited.values_list("branch_id", flat=True)), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual({row.metadata["amount"] for row in audited}, {170_000})
        # Both sides hold the same two-party figures, and name no third branch.
        figures = {(row.metadata["owed"], row.metadata["credit"], row.metadata["deferred"],
                    row.metadata["net"], row.metadata["from_branch_id"], row.metadata["to_branch_id"])
                   for row in audited}
        self.assertEqual(figures, {(170_000, 0, 0, 170_000, self.ikeja.pk, self.lekki.pk)})

    def test_a_voided_move_gives_its_key_up_and_the_redone_run_moves_the_new_bill(self):
        key = f"fee-run:F1:2026-T1:C{self.tunde.pk}:B{self.ikeja.pk}-B{self.lekki.pk}"
        first = self.move(move_key=key)
        void_inter_branch_transfer(InterBranchTransfer.objects.get(pk=first.transfer_id), date=JAN_20)
        void_invoice(self.first, date=JAN_20)
        rebilled = self.posted_invoice(self.tunde, self.ikeja)

        again = self.move(move_key=key)
        retried = self.move(move_key=key)

        self.assertNotEqual(again.transfer_id, first.transfer_id)
        self.assertEqual(set(again.invoice_ids), {rebilled.pk, self.second.pk})
        self.assertEqual(retried, again)
        rebilled.refresh_from_db()
        self.assertEqual(rebilled.branch_id, self.lekki.pk)
        self.assertEqual(net(self.ar, self.ikeja), 0)
        self.assertEqual(net(self.ar, self.lekki), 170_000)

    def test_a_retry_that_raced_the_move_returns_it_and_moves_nothing_more(self):
        from unittest import mock

        from . import inter_branch

        key = "fee-run:F1:2026-T1:race"
        first = self.move(move_key=key)
        later = self.posted_invoice(self.tunde, self.ikeja)
        real, calls = inter_branch._live_move, []

        def unseen_until_the_insert(entity, move_key):
            calls.append(move_key)
            return None if len(calls) <= 2 else real(entity, move_key)

        with mock.patch.object(inter_branch, "_live_move", side_effect=unseen_until_the_insert):
            raced = self.move(move_key=key)

        self.assertEqual(raced, first)
        self.assertEqual(len(calls), 3)
        later.refresh_from_db()
        self.assertEqual(later.branch_id, self.ikeja.pk)
        self.assertEqual(InterBranchTransfer.objects.filter(move_key=key).count(), 1)

    def test_a_second_call_for_the_same_move_moves_nothing(self):
        first = self.move(move_key="move-1")

        again = self.move()
        keyed = self.move(move_key="move-1")

        self.assertEqual((again.transfer_id, again.amount), (None, 0))
        self.assertEqual(keyed, first)
        self.assertEqual(InterBranchTransfer.objects.count(), 1)

    def test_a_receipt_at_lekki_settles_the_moved_invoice(self):
        self.move()
        receipt = Payment.objects.create(
            entity=self.books, branch=self.lekki, customer=self.tunde, payment_date=JAN_20,
            amount=170_000, deposit_account=self.lekki_bank.gl_account,
        )

        post_payment(receipt)

        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual((self.first.balance_due, self.second.balance_due), (0, 0))
        self.assertEqual(net(self.ar, self.lekki), 0)

    def test_a_moved_invoice_is_not_voided_on_its_own_and_the_move_undoes_until_it_is_paid(self):
        moved = self.move()
        transfer = InterBranchTransfer.objects.get(pk=moved.transfer_id)

        with self.assertRaisesMessage(PostingError, "Void that move first"):
            void_invoice(self.first)
        void_inter_branch_transfer(transfer, date=JAN_20)

        self.first.refresh_from_db()
        self.assertEqual(self.first.branch_id, self.ikeja.pk)
        self.assertEqual(net(self.ar, self.lekki), 0)
        again = self.move()
        receipt = Payment.objects.create(
            entity=self.books, branch=self.lekki, customer=self.tunde, payment_date=JAN_20,
            amount=10_000, deposit_account=self.lekki_bank.gl_account,
        )
        post_payment(receipt)
        with self.assertRaisesMessage(InterBranchError, "cannot be undone"):
            void_inter_branch_transfer(InterBranchTransfer.objects.get(pk=again.transfer_id))

    def test_only_a_whole_school_caller_moves_a_balance_through_the_api(self):
        body = {"customer": "TUNDE", "from_branch": self.ikeja.pk, "to_branch": self.lekki.pk,
                "move_date": "2026-01-15"}

        refused = self.client_for(self.ikeja, self.lekki).post(
            self.url("inter-branch-transfers/receivable-moves/"), body, format="json")
        moved = self.client_for(None).post(
            self.url("inter-branch-transfers/receivable-moves/"), body, format="json")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(moved.status_code, 201, moved.data)
        self.assertEqual(moved.data["data"]["amount"], 170_000)


class WholeBalanceMoveTests(_InterBranchFixture):
    """Tunde also owes a 25k debit note and has 40k of an unapplied payment at Ikeja."""

    @classmethod
    def setUpTestData(cls):
        from .credit_notes import post_credit_note

        super().setUpTestData()
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.first = cls.posted_invoice(cls.tunde, cls.ikeja)
        cls.debit_note = CreditNote.objects.create(
            entity=cls.books, customer=cls.tunde, branch=cls.ikeja, kind=CreditNoteKind.DEBIT,
            note_date=datetime.date(2026, 1, 11), reason="Lab fee missed",
        )
        CreditNoteLine.objects.create(
            note=cls.debit_note, line_no=1, quantity=1, unit_price=25_000,
            revenue_account=Account.objects.get(entity=cls.books, code="4100"),
        )
        post_credit_note(cls.debit_note)
        cls.advance = Payment.objects.create(
            entity=cls.books, branch=cls.ikeja, customer=cls.tunde, payment_date=datetime.date(2026, 1, 12),
            amount=40_000, deposit_account=cls.ikeja_bank.gl_account,
        )
        post_payment(cls.advance, auto_allocate=False)

    def move(self):
        return transfer_open_receivables(self.tunde, self.ikeja, self.lekki, None, move_date=JAN_15)

    def test_the_debit_note_and_the_credit_move_with_the_invoices(self):
        moved = self.move()

        self.assertEqual((moved.amount, moved.invoice_count, moved.debit_note_count, moved.credit_count,
                          moved.credit_amount), (85_000, 1, 1, 1, 40_000))
        self.debit_note.refresh_from_db()
        self.advance.refresh_from_db()
        self.assertEqual(self.debit_note.branch_id, self.lekki.pk)
        self.assertEqual((self.advance.branch_id, self.advance.transferred_amount), (self.ikeja.pk, 40_000))
        transfer = InterBranchTransfer.objects.get(pk=moved.transfer_id)
        self.assertEqual((transfer.receipt.branch_id, transfer.receipt.amount), (self.lekki.pk, 40_000))
        credit = Account.objects.get(entity=self.books, code="2140")
        self.assertEqual((net(self.ar, self.ikeja), net(credit, self.ikeja)), (0, 0))
        self.assertEqual(net(self.ar, self.lekki), 85_000)
        self.assertEqual(net(credit, self.lekki), 0)
        self.assertEqual(net(self.ib, self.lekki, self.ikeja), -85_000)
        self.assertEqual(net(self.ib), 0)
        with self.assertRaisesMessage(PostingError, "Void that move first"):
            void_payment(self.advance)

    def test_voiding_the_move_gives_everything_back(self):
        moved = self.move()

        void_inter_branch_transfer(InterBranchTransfer.objects.get(pk=moved.transfer_id), date=JAN_20)

        self.debit_note.refresh_from_db()
        self.advance.refresh_from_db()
        self.first.refresh_from_db()
        self.assertEqual((self.debit_note.branch_id, self.first.branch_id), (self.ikeja.pk, self.ikeja.pk))
        self.assertEqual(self.advance.transferred_amount, 0)
        self.assertEqual(self.first.balance_due, 100_000)
        self.assertEqual(net(self.ar, self.lekki), 0)
        self.assertEqual(net(self.ib), 0)


class _TermFixture(_InterBranchFixture):
    """Tunde's 400k second term runs 20 January to 30 April, billed at Ikeja: 100k a month.

    February to April are open at Corona and at Single Site, whose own pupil Kemi
    is billed the same term at its one branch.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        FiscalPeriod.objects.filter(entity=cls.books, period_no=2).update(status=PeriodStatus.OPEN)
        for books in (cls.books, cls.solo_books):
            year = FiscalYear.objects.get(entity=books)
            for month in (2, 3, 4) if books == cls.solo_books else (3, 4):
                FiscalPeriod.objects.create(
                    entity=books, fiscal_year=year, period_no=month, name=f"M{month} 2026",
                    start_date=datetime.date(2026, month, 1),
                    end_date=datetime.date(2026, month, {2: 28, 4: 30}.get(month, 31)),
                )
        cls.deferred = Account.objects.get(entity=cls.books, code="2160")
        cls.revenue = Account.objects.get(entity=cls.books, code="4100")
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.term = cls.term_invoice(cls.books, cls.tunde, cls.ikeja)
        cls.kemi = cls.customer(cls.solo_books, "KEMI", cls.solo_main)
        cls.solo_term = cls.term_invoice(cls.solo_books, cls.kemi, cls.solo_main)

    @classmethod
    def term_invoice(cls, books, customer, branch):
        invoice = Invoice.objects.create(
            entity=books, customer=customer, branch=branch,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=400_000,
            revenue_account=Account.objects.get(entity=books, code="4100"),
            service_start=datetime.date(2026, 1, 20), service_end=datetime.date(2026, 4, 30),
        )
        post_invoice(invoice)
        return invoice


class UnearnedIncomeMoveTests(_TermFixture):
    """Tunde moves on 25 January, partway through his term."""

    def test_income_not_yet_earned_moves_and_is_recognised_at_the_new_branch(self):
        from .deferred_income import release_deferred_income

        moved = transfer_open_receivables(
            self.tunde, self.ikeja, self.lekki, None, move_date=datetime.date(2026, 1, 25))
        release_deferred_income(self.books, up_to=datetime.date(2026, 3, 31))

        deferred = Account.objects.get(entity=self.books, code="2160")
        revenue = Account.objects.get(entity=self.books, code="4100")
        self.assertEqual((moved.amount, moved.deferred_amount), (400_000, 300_000))
        self.assertEqual(net(self.ar, self.lekki), 400_000)
        self.assertEqual(net(self.ib, self.lekki, self.ikeja), -100_000)
        self.assertEqual(net(self.ib), 0)
        self.assertEqual(net(revenue, self.ikeja), -100_000)
        self.assertEqual(net(revenue, self.lekki), -200_000)
        self.assertEqual(net(deferred, self.ikeja), 0)
        self.assertEqual(net(deferred, self.lekki), -100_000)


JAN_28 = datetime.date(2026, 1, 28)
FEB_3 = datetime.date(2026, 2, 3)
APR_30 = datetime.date(2026, 4, 30)


class IncomeGivenBackTests(_TermFixture):
    """Tunde moves to Lekki on 25 January; Lekki then credits, concedes or writes off his term.

    The move leaves January's 100k at Ikeja, which earned it, and Lekki owes
    Ikeja that 100k. Whatever a credit note or concession at Lekki takes back of
    January is taken from Ikeja, through the inter-branch account, so neither
    branch's books carry the other's income and the pair still agrees. A
    write-off is Lekki's loss alone: January stays earned at Ikeja, whenever the
    release runs.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.moved = transfer_open_receivables(
            cls.tunde, cls.ikeja, cls.lekki, None, move_date=datetime.date(2026, 1, 25))

    def credit(self, amount, on, *, invoice=None, branch=None):
        from .credit_notes import post_credit_note

        invoice = Invoice.objects.get(pk=(invoice or self.term).pk)
        note = CreditNote.objects.create(
            entity=invoice.entity, customer=invoice.customer, branch=branch or self.lekki,
            kind=CreditNoteKind.CREDIT, note_date=on, invoice=invoice, reason="Term cancelled",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, quantity=1, unit_price=amount,
            revenue_account=Account.objects.get(entity=invoice.entity, code="4100"),
        )
        post_credit_note(note)
        note.refresh_from_db()
        return note

    def given_back(self):
        return InterBranchTransfer.objects.filter(kind=InterBranchTransferKind.INCOME_GIVEN_BACK)

    def assert_pairs_agree(self, period_no=4):
        from .inter_branch import inter_branch_close_check

        period = FiscalPeriod.objects.get(entity=self.books, period_no=period_no)
        check = inter_branch_close_check(self.books, period)
        self.assertTrue(check.passed, check.detail)
        self.assertEqual(net(self.ib), 0)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), -net(self.ib, self.lekki, self.ikeja))

    def assert_branch_books(self, *, deferred, revenue, lekki_owes_ikeja):
        """``deferred`` and ``revenue`` are ``(Ikeja, Lekki)``, as debits less credits."""
        self.assertEqual((net(self.deferred, self.ikeja), net(self.deferred, self.lekki)), deferred)
        self.assertEqual((net(self.revenue, self.ikeja), net(self.revenue, self.lekki)), revenue)
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), lekki_owes_ikeja)
        self.assert_pairs_agree()

    def test_cancelling_the_whole_term_before_january_is_released_takes_january_back_from_ikeja(self):
        from .deferred_income import release_deferred_income
        from .inter_branch import pair_balances

        note = self.credit(400_000, JAN_28)
        released = release_deferred_income(self.books, up_to=APR_30)

        self.assertEqual(released, [])
        self.assert_branch_books(deferred=(0, 0), revenue=(0, 0), lekki_owes_ikeja=0)
        self.assertEqual(net(self.ar, self.lekki), 0)
        self.assertEqual(pair_balances(self.books)["pairs"], [])
        lines = {(line.account.code, line.counterparty_branch_id): line.debit
                 for line in note.journal.lines.filter(debit__gt=0)}
        self.assertEqual(lines, {("2160", None): 300_000, ("1260", self.ikeja.pk): 100_000})
        (transfer,) = self.given_back()
        self.assertEqual((transfer.branch_id, transfer.to_branch_id, transfer.amount),
                         (self.lekki.pk, self.ikeja.pk, 100_000))
        legs = {leg.role: leg for leg in transfer.legs.all()}
        self.assertIsNone(legs[InterBranchLegRole.SENDING].journal_id)
        ikeja_side = legs[InterBranchLegRole.RECEIVING].journal
        self.assertEqual(ikeja_side.branch_id, self.ikeja.pk)
        self.assertEqual(
            sorted((line.account.code, line.debit, line.credit, line.counterparty_branch_id)
                   for line in ikeja_side.lines.all()),
            [("1260", 0, 100_000, self.lekki.pk), ("2160", 100_000, 0, None)],
        )

    def test_crediting_february_to_april_leaves_january_earned_at_ikeja(self):
        from .deferred_income import release_deferred_income

        self.credit(300_000, JAN_28)

        self.assert_branch_books(deferred=(-100_000, 0), revenue=(0, 0), lekki_owes_ikeja=100_000)
        release_deferred_income(self.books, up_to=APR_30)
        self.assert_branch_books(deferred=(0, 0), revenue=(-100_000, 0), lekki_owes_ikeja=100_000)
        self.assertFalse(self.given_back().exists())
        self.assertEqual(net(self.ar, self.lekki), 100_000)

    def test_cancelling_after_january_was_released_takes_the_revenue_back_from_ikeja(self):
        from .deferred_income import release_deferred_income

        release_deferred_income(self.books, up_to=datetime.date(2026, 1, 31))
        self.credit(400_000, FEB_3)

        self.assert_branch_books(deferred=(0, 0), revenue=(0, 0), lekki_owes_ikeja=0)
        (transfer,) = self.given_back()
        ikeja_side = transfer.legs.get(role=InterBranchLegRole.RECEIVING).journal
        self.assertEqual(
            sorted((line.account.code, line.debit, line.credit) for line in ikeja_side.lines.all()),
            [("1260", 0, 100_000), ("4100", 100_000, 0)],
        )

    def test_two_credit_notes_take_released_months_latest_first_and_never_twice(self):
        from .deferred_income import release_deferred_income

        release_deferred_income(self.books, up_to=datetime.date(2026, 2, 28))
        self.credit(250_000, datetime.date(2026, 3, 2))

        self.assert_branch_books(deferred=(0, 0), revenue=(-100_000, -50_000), lekki_owes_ikeja=100_000)
        self.credit(150_000, datetime.date(2026, 3, 3))
        self.assert_branch_books(deferred=(0, 0), revenue=(0, 0), lekki_owes_ikeja=0)

    def test_voiding_the_credit_note_gives_ikeja_january_back(self):
        from .deferred_income import release_deferred_income
        from .voids import void_credit_note

        note = self.credit(400_000, JAN_28)

        void_credit_note(note, date=datetime.date(2026, 1, 29))

        (transfer,) = self.given_back()
        self.assertEqual(transfer.status, DocumentStatus.REVERSED)
        self.assert_branch_books(deferred=(-100_000, -300_000), revenue=(0, 0), lekki_owes_ikeja=100_000)
        release_deferred_income(self.books, up_to=APR_30)
        self.assert_branch_books(deferred=(0, 0), revenue=(-100_000, -300_000), lekki_owes_ikeja=100_000)

    def test_the_move_and_the_income_given_back_are_voided_only_through_the_credit_note(self):
        note = self.credit(400_000, JAN_28)
        (transfer,) = self.given_back()

        with self.assertRaisesMessage(InterBranchError, "cannot be undone"):
            void_inter_branch_transfer(InterBranchTransfer.objects.get(pk=self.moved.transfer_id))
        with self.assertRaisesMessage(InterBranchError, f"credit note {note.document_number}"):
            void_inter_branch_transfer(transfer)
        with self.assertRaisesMessage(PostingError, "cannot be reversed on its own"):
            from .posting import reverse_journal

            reverse_journal(transfer.legs.get(role=InterBranchLegRole.RECEIVING).journal)

    def write_off_and_release(self, on):
        """Lekki writes Tunde's term off on ``on``; every month due is then released."""
        from .credit_notes import write_off_invoice
        from .deferred_income import release_deferred_income

        write_off_invoice(Invoice.objects.get(pk=self.term.pk), write_off_date=on)
        release_deferred_income(self.books, up_to=APR_30)

    def assert_lekki_bears_the_write_off(self):
        """January stays earned at Ikeja; Lekki books the bad debt and still owes for it."""
        bad_debt = Account.objects.get(entity=self.books, code="5350")
        self.assert_branch_books(deferred=(0, 0), revenue=(-100_000, 0), lekki_owes_ikeja=100_000)
        self.assertEqual((net(bad_debt, self.ikeja), net(bad_debt, self.lekki)), (0, 100_000))
        self.assertEqual(net(self.ar, self.lekki), 0)
        self.assertFalse(self.given_back().exists())

    def test_a_write_off_before_january_is_released_leaves_january_earned_at_ikeja(self):
        self.write_off_and_release(JAN_28)

        self.assert_lekki_bears_the_write_off()

    def test_a_write_off_after_january_is_released_books_the_same(self):
        from .deferred_income import release_deferred_income

        release_deferred_income(self.books, up_to=datetime.date(2026, 1, 31))
        self.write_off_and_release(FEB_3)

        self.assert_lekki_bears_the_write_off()

    def test_a_concession_at_lekki_gives_back_ikejas_january_and_its_void_restores_it(self):
        from .installments import post_concession
        from .models import Concession
        from .voids import void_concession

        concession = Concession.objects.create(
            entity=self.books, customer=self.tunde, invoice=Invoice.objects.get(pk=self.term.pk),
            branch=self.lekki, concession_date=JAN_28, amount=400_000,
        )
        post_concession(concession)

        self.assert_branch_books(deferred=(0, 0), revenue=(0, 0), lekki_owes_ikeja=0)
        void_concession(concession, date=JAN_28)
        self.assert_branch_books(deferred=(-100_000, -300_000), revenue=(0, 0), lekki_owes_ikeja=100_000)
        self.assertEqual(self.given_back().get().status, DocumentStatus.REVERSED)

    def test_a_single_branch_school_credits_its_whole_term_as_before(self):
        from .deferred_income import release_deferred_income
        from .models import DeferredIncomeUnwind

        deferred = Account.objects.get(entity=self.solo_books, code="2160")
        revenue = Account.objects.get(entity=self.solo_books, code="4100")
        release_deferred_income(self.solo_books, up_to=datetime.date(2026, 1, 31))

        note = self.credit(400_000, FEB_3, invoice=self.solo_term, branch=self.solo_main)

        self.assertEqual(
            sorted((line.account.code, line.debit, line.counterparty_branch_id)
                   for line in note.journal.lines.filter(debit__gt=0)),
            [("2160", 300_000, None), ("4100", 100_000, None)],
        )
        self.assertEqual((net(deferred), net(revenue)), (0, 0))
        self.assertFalse(ledger_lines(self.solo_books).filter(account__code="1260").exists())
        self.assertFalse(InterBranchTransfer.objects.filter(entity=self.solo_books).exists())
        self.assertFalse(DeferredIncomeUnwind.objects.filter(after_release=True).exists())


class MovedBillCreditTests(_InterBranchFixture):
    """Tunde's 100k textbook bill (plus 7.5k VAT) is raised at Ikeja and moves to Lekki with him.

    The books never arrive. Whatever Lekki credits or concedes on the bill comes
    out of Ikeja's revenue and output VAT, which booked it, through the
    inter-branch account; Lekki's revenue is untouched and what it owes Ikeja
    for the bill falls by the same amount.
    """

    @classmethod
    def setUpTestData(cls):
        from .models import TaxCode

        super().setUpTestData()
        cls.vat = TaxCode.objects.get(entity=cls.books, code="VAT-STD")
        cls.revenue = Account.objects.get(entity=cls.books, code="4100")
        cls.output_vat = Account.objects.get(entity=cls.books, code="2200")
        cls.tunde = cls.customer(cls.books, "TUNDE", cls.ikeja)
        cls.textbooks = cls.taxed_invoice(cls.tunde, cls.ikeja)
        cls.moved = transfer_open_receivables(cls.tunde, cls.ikeja, cls.lekki, None, move_date=JAN_15)

    @classmethod
    def taxed_invoice(cls, customer, branch):
        invoice = Invoice.objects.create(
            entity=cls.books, customer=customer, branch=branch,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=100_000,
            revenue_account=cls.revenue, tax_code=cls.vat,
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def credit(self, amount, *, invoice=None, branch=None):
        from .credit_notes import post_credit_note

        invoice = Invoice.objects.get(pk=(invoice or self.textbooks).pk)
        note = CreditNote.objects.create(
            entity=self.books, customer=invoice.customer, branch=branch or self.lekki,
            kind=CreditNoteKind.CREDIT, note_date=JAN_20, invoice=invoice,
            reason="Books never delivered",
        )
        CreditNoteLine.objects.create(
            note=note, line_no=1, quantity=1, unit_price=amount, revenue_account=self.revenue,
            tax_code=self.vat,
        )
        post_credit_note(note)
        note.refresh_from_db()
        return note

    def vat_shares(self):
        """Each branch's output VAT on January's return, ``{branch_id: kobo}``."""
        from .models import TaxObligation
        from .tax_filing import prepare_filing

        filing = prepare_filing(
            TaxObligation.objects.get(entity=self.books, code="VAT"),
            period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),
        )
        return {share.branch_id: int(share.gross_liability) for share in filing.shares.all()}

    def assert_books(self, *, ikeja_revenue, ikeja_vat, lekki_owes_ikeja):
        """Ikeja's revenue and output VAT as credits; Lekki's are always untouched."""
        from .inter_branch import inter_branch_close_check

        self.assertEqual((-net(self.revenue, self.ikeja), -net(self.output_vat, self.ikeja)),
                         (ikeja_revenue, ikeja_vat))
        self.assertEqual((net(self.revenue, self.lekki), net(self.output_vat, self.lekki)), (0, 0))
        self.assertEqual(net(self.ib, self.ikeja, self.lekki), lekki_owes_ikeja)
        self.assertEqual(net(self.ar, self.lekki), lekki_owes_ikeja)
        check = inter_branch_close_check(self.books, FiscalPeriod.objects.get(entity=self.books, period_no=1))
        self.assertTrue(check.passed, check.detail)
        self.assertEqual(net(self.ib), 0)

    def test_crediting_the_whole_bill_takes_revenue_and_vat_back_from_ikeja(self):
        note = self.credit(100_000)

        self.assert_books(ikeja_revenue=0, ikeja_vat=0, lekki_owes_ikeja=0)
        self.assertEqual(self.vat_shares(), {self.ikeja.pk: 0})
        self.assertEqual(
            [(line.account.code, line.debit, line.credit, line.counterparty_branch_id)
             for line in note.journal.lines.order_by("line_no")],
            [("1260", 107_500, 0, self.ikeja.pk), ("1200", 0, 107_500, None)],
        )
        transfer = InterBranchTransfer.objects.get(kind=InterBranchTransferKind.INCOME_GIVEN_BACK)
        ikeja_side = transfer.legs.get(role=InterBranchLegRole.RECEIVING).journal
        self.assertEqual(
            sorted((line.account.code, line.debit, line.credit) for line in ikeja_side.lines.all()),
            [("1260", 0, 107_500), ("2200", 7_500, 0), ("4100", 100_000, 0)],
        )

    def test_a_part_credit_lowers_ikejas_revenue_vat_share_and_what_lekki_owes(self):
        self.credit(40_000)

        self.assert_books(ikeja_revenue=60_000, ikeja_vat=4_500, lekki_owes_ikeja=64_500)
        self.assertEqual(self.vat_shares(), {self.ikeja.pk: 4_500})

    def test_voiding_the_credit_note_restores_ikejas_income_and_the_balance(self):
        from .voids import void_credit_note

        note = self.credit(100_000)

        void_credit_note(note, date=JAN_20)

        self.assert_books(ikeja_revenue=100_000, ikeja_vat=7_500, lekki_owes_ikeja=107_500)
        self.assertEqual(self.vat_shares(), {self.ikeja.pk: 7_500})
        self.assertEqual(
            InterBranchTransfer.objects.get(kind=InterBranchTransferKind.INCOME_GIVEN_BACK).status,
            DocumentStatus.REVERSED,
        )

    def test_a_concession_at_lekki_comes_out_of_ikejas_allowances_and_its_void_restores_it(self):
        from .installments import post_concession
        from .models import Concession
        from .voids import void_concession

        allowances = Account.objects.get(entity=self.books, code="4910")
        concession = Concession.objects.create(
            entity=self.books, customer=self.tunde, invoice=Invoice.objects.get(pk=self.textbooks.pk),
            branch=self.lekki, concession_date=JAN_20, amount=30_000,
        )
        post_concession(concession)

        self.assertEqual((net(allowances, self.ikeja), net(allowances, self.lekki)), (30_000, 0))
        self.assert_books(ikeja_revenue=100_000, ikeja_vat=7_500, lekki_owes_ikeja=77_500)
        void_concession(concession, date=JAN_20)
        self.assertEqual(net(allowances), 0)
        self.assert_books(ikeja_revenue=100_000, ikeja_vat=7_500, lekki_owes_ikeja=107_500)

    def test_a_bill_that_never_moved_is_credited_at_its_own_branch_as_before(self):
        yinka = self.customer(self.books, "YINKA", self.lekki)
        own = self.taxed_invoice(yinka, self.lekki)

        note = self.credit(40_000, invoice=own)

        self.assertEqual(
            [(line.account.code, line.debit, line.credit, line.counterparty_branch_id)
             for line in note.journal.lines.order_by("line_no")],
            [("4100", 40_000, 0, None), ("2200", 3_000, 0, None), ("1200", 0, 43_000, None)],
        )
        self.assertFalse(InterBranchTransfer.objects.filter(
            kind=InterBranchTransferKind.INCOME_GIVEN_BACK).exists())


class InterBranchCloseCheckTests(_InterBranchFixture):
    """Ikeja lends Lekki 1m; then a broken journal books one side only."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.january = FiscalPeriod.objects.get(entity=cls.books, period_no=1)
        book_goods_transfer(
            cls.books, from_branch=cls.ikeja, to_branch=cls.lekki, amount=1_000_000_00,
            transfer_date=JAN_15, inventory_account=Account.objects.get(entity=cls.books, code="1400"),
            purpose="Textbooks",
        )

    def one_sided(self):
        entry = JournalEntry.objects.create(
            entity=self.books, branch=self.ikeja, date=JAN_15, period=self.january,
            source=JournalSource.SYSTEM,
        )
        JournalLine.objects.create(entry=entry, account=self.ib, debit=5_000, credit=0,
                                   counterparty_branch=self.lekki, line_no=1)
        JournalLine.objects.create(entry=entry, account=self.audit_fee, debit=0, credit=5_000, line_no=2)
        post_journal(entry)

    def test_paired_balances_pass_the_close(self):
        from .close import close_checklist

        item = next(i for i in close_checklist(self.books, self.january).items
                    if i.name == "inter_branch_balanced")

        self.assertTrue(item.passed, item.detail)
        self.assertTrue(item.blocking)

    def test_a_one_sided_balance_blocks_the_tenant_close_and_warns_the_branches_in_the_pair(self):
        from .close import close_checklist
        from .inter_branch import inter_branch_close_check

        self.one_sided()

        tenant = close_checklist(self.books, self.january)
        ikeja = inter_branch_close_check(self.books, self.january, branch=self.ikeja)
        yaba = inter_branch_close_check(self.books, self.january, branch=self.yaba)

        self.assertIn("inter_branch_balanced", [i.name for i in tenant.failures])
        self.assertFalse(tenant.passed)
        self.assertEqual((ikeja.passed, ikeja.blocking), (False, False))
        self.assertIn("disagree", ikeja.detail)
        self.assertTrue(yaba.passed)


class GoodsTransferTests(_InterBranchFixture):
    """Ikeja's central store sends Lekki textbooks at moving-average cost."""

    @classmethod
    def setUpTestData(cls):
        from vs_procurement.models import StockItem, StockLocation
        from vs_procurement.stock import receive_stock

        super().setUpTestData()
        cls.inventory = Account.objects.get(entity=cls.books, code="1400")
        cls.central = StockLocation.objects.create(
            entity=cls.books, branch=cls.ikeja, code="IKJ-MAIN", name="Ikeja store", is_default=True)
        cls.ikeja_lab = StockLocation.objects.create(
            entity=cls.books, branch=cls.ikeja, code="IKJ-LAB", name="Ikeja lab")
        cls.lekki_store = StockLocation.objects.create(
            entity=cls.books, branch=cls.lekki, code="LKK-MAIN", name="Lekki store")
        cls.books_item = StockItem.objects.create(
            entity=cls.books, code="TXT", name="Textbook", inventory_account=cls.inventory)
        receive_stock(cls.books_item, quantity=Decimal(10), value=50_000_00, movement_date=JAN_15,
                      location=cls.central)

    def test_goods_to_another_branch_book_both_inventories_and_lekki_owes_the_cost(self):
        from vs_procurement.stock import transfer_stock

        out = transfer_stock(self.books_item, quantity=Decimal(4), movement_date=JAN_20,
                             from_location=self.central, to_location=self.lekki_store)

        self.assertEqual((out.value_amount, out.paired_movement.value_amount), (-20_000_00, 20_000_00))
        self.assertEqual(out.journal.branch_id, self.ikeja.pk)
        self.assertEqual(out.paired_movement.journal.branch_id, self.lekki.pk)
        self.assertEqual(net(self.inventory, self.ikeja), -20_000_00)
        self.assertEqual(net(self.inventory, self.lekki), 20_000_00)
        self.assertEqual(net(self.ib, self.lekki, self.ikeja), -20_000_00)
        self.assertEqual(out.inter_branch_transfer.kind, InterBranchTransferKind.GOODS)

    def test_a_move_between_two_stores_of_one_branch_posts_nothing(self):
        from vs_procurement.stock import transfer_stock

        before = JournalEntry.objects.count()
        out = transfer_stock(self.books_item, quantity=Decimal(2), movement_date=JAN_20,
                             from_location=self.central, to_location=self.ikeja_lab)

        self.assertIsNone(out.inter_branch_transfer)
        self.assertEqual(JournalEntry.objects.count(), before)
        self.assertEqual(out.paired_movement.balance_qty, Decimal(2))

    def test_the_api_sends_goods_from_the_callers_own_store(self):
        response = self.client_for(self.ikeja, keys=("procurement.stock.issue",)).post(
            f"/v1/procurement/stock-items/{self.books_item.pk}/transfer/?entity={self.books.code}",
            {"quantity": "4", "location": self.central.pk, "to_location": "LKK-MAIN",
             "movement_date": "2026-01-20"}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(response.data["data"]["inter_branch_transfer_id"])


class OneBranchTests(_InterBranchFixture):
    """Single Site has one branch, so there is nobody to transfer to."""

    def test_every_write_refuses_with_the_reason_and_nothing_books(self):
        client = self.client_for(None, tenant=self.solo_tenant)
        books = self.solo_books

        sent = client.post(self.url("inter-branch-transfers/", books), {
            "from_bank_account": self.solo_bank.pk, "to_branch": self.solo_main.pk, "amount": 1_000,
            "purpose": "x",
        }, format="json")
        asked = client.post(self.url("inter-branch-transfers/requests/", books),
                            {"from_branch": self.solo_main.pk, "amount": 1_000, "purpose": "x"},
                            format="json")
        recharged = client.post(self.url("recharges/", books),
                                {"amount": 1_000, "narration": "x", "expense_account": "5300"},
                                format="json")
        balances = client.get(self.url("inter-branch-balances/", books))

        for response in (sent, asked, recharged):
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn("only one branch", str(response.data))
        self.assertEqual(balances.status_code, 200, balances.data)
        self.assertEqual(balances.data["data"]["pairs"], [])
        self.assertFalse(InterBranchTransfer.objects.exists())
        self.assertFalse(ledger_lines(books).filter(account__code="1260").exists())

    def test_the_services_refuse_too(self):
        customer = self.customer(self.solo_books, "SOLO1", self.solo_main)

        with self.assertRaises(InterBranchUnavailableError):
            transfer_open_receivables(customer, self.solo_main, self.solo_main, None)

    def test_a_store_move_at_one_branch_posts_nothing(self):
        from vs_procurement.models import StockItem, StockLocation
        from vs_procurement.stock import receive_stock, transfer_stock

        main = StockLocation.objects.create(entity=self.solo_books, code="MAIN", name="Main", is_default=True)
        annex = StockLocation.objects.create(entity=self.solo_books, branch=self.solo_main, code="ANX",
                                             name="Annex")
        item = StockItem.objects.create(entity=self.solo_books, code="PEN", name="Pen",
                                        inventory_account=Account.objects.get(entity=self.solo_books, code="1400"))
        receive_stock(item, quantity=Decimal(5), value=5_000, movement_date=JAN_15, location=main)

        out = transfer_stock(item, quantity=Decimal(5), movement_date=JAN_15, from_location=main,
                             to_location=annex)

        self.assertIsNone(out.inter_branch_transfer)
        self.assertIsNone(out.journal)
