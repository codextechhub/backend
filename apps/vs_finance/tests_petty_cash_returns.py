"""Petty cash goes back to the bank: a float is cut, or a fund is closed.

Corona runs Ikeja, Lekki and Yaba. Ikeja keeps a ₦100,000 petty cash float with
Mrs Adeyemi as custodian, funded from Ikeja's own bank account. The school cuts it
to ₦50,000, and later closes the fund when Ikeja moves to card payments. Each time
the custodian counts the tin and the cash goes back into Ikeja's bank, never
Lekki's. A count that differs from the books goes to the cash over and short
account with a reason. The bank side is an ordinary deposit on Ikeja's register
and reconciliation, and a return can be voided until the bank has matched it.

The single-branch school is here because a fund and an account raised before
either carried a branch belong to its only branch, and the return must work
there without anybody naming one.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from vs_finance.constants import (
    DocumentStatus,
    FinanceAuditAction,
    PeriodStatus,
    PettyCashReturnKind,
)
from vs_finance.exceptions import PeriodClosedError, PettyCashError
from vs_finance.models import (
    Account,
    FinanceAuditLog,
    FiscalPeriod,
    JournalLine,
    PettyCashFund,
    PettyCashReturn,
    PettyCashVoucher,
    PettyCashVoucherLine,
)
from vs_finance.petty_cash import (
    cancel_voucher,
    change_fund_details,
    establish_fund,
    gl_cash_on_hand,
    post_petty_cash_return,
    post_voucher,
    reopen_fund,
    replenish_fund,
    void_petty_cash_return,
    void_voucher,
)

from .tests_bank_account_reach import _bank
from .tests_branch_scope import _FinanceBranchFixture

JAN_2 = datetime.date(2026, 1, 2)
JAN_10 = datetime.date(2026, 1, 10)
JAN_20 = datetime.date(2026, 1, 20)
NAIRA = 100
FLOAT = 100_000 * NAIRA


class _PettyCashReturnFixture(_FinanceBranchFixture):
    """Ikeja's ₦100,000 float, Ikeja's and Lekki's banks, and a Lekki fund."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.books
        cls.ikeja_bank = _bank(e, "Ikeja Operations", cls.ikeja, "40")
        cls.lekki_bank = _bank(e, "Lekki Operations", cls.lekki, "41")
        cls.custodian = cls.user_for(cls.tenant, "adeyemi@corona.test")
        cls.fund = PettyCashFund.objects.create(
            entity=e, branch=cls.ikeja, name="Ikeja front desk",
            gl_account=Account.objects.get(entity=e, code="1110"),
            custodian=cls.custodian, float_amount=FLOAT,
        )
        establish_fund(cls.fund, bank_account=cls.ikeja_bank, amount=FLOAT, date=JAN_2)
        lekki_gl = Account.objects.create(
            entity=e, code="1111", name="Petty Cash Lekki",
            account_type=Account.objects.get(entity=e, code="1110").account_type,
            is_postable=True,
        )
        cls.lekki_fund = PettyCashFund.objects.create(
            entity=e, branch=cls.lekki, name="Lekki front desk",
            gl_account=lekki_gl, float_amount=20_000 * NAIRA,
        )
        establish_fund(
            cls.lekki_fund, bank_account=cls.lekki_bank, amount=20_000 * NAIRA, date=JAN_2)

    def make_return(self, kind, *, counted, amount=None, new_float=None, bank="ikeja",
                    reason="", fund=None, date=JAN_10, save=True):
        """A draft return of ``fund`` counted at ``counted`` kobo against its books today."""
        fund = fund or self.fund
        fund.refresh_from_db()
        closing = kind == PettyCashReturnKind.CLOSE
        ret = PettyCashReturn(
            entity=fund.entity, branch=fund.branch, fund=fund, kind=kind,
            bank_account={"ikeja": self.ikeja_bank, "lekki": self.lekki_bank, None: None}[bank],
            return_date=date, counted_amount=counted, book_balance=gl_cash_on_hand(fund),
            amount=counted if closing else amount,
            previous_float_amount=fund.float_amount,
            new_float_amount=0 if closing else new_float,
            difference_reason=reason,
        )
        if save:
            ret.save()
        return ret

    def spend(self, amount, *, post=True, fund=None):
        fund = fund or self.fund
        voucher = PettyCashVoucher.objects.create(
            entity=fund.entity, branch=fund.branch, fund=fund, voucher_date=JAN_10,
            payee="Corner Shop",
        )
        PettyCashVoucherLine.objects.create(
            voucher=voucher, line_no=1, quantity=1, unit_price=amount,
            expense_account=Account.objects.get(entity=fund.entity, code="5300"),
        )
        if post:
            post_voucher(voucher)
        return voucher

    def lines(self, ret):
        """``{account code: (debit, credit)}`` of the return's journal."""
        return {
            line.account.code: (line.debit, line.credit)
            for line in ret.journal.lines.select_related("account")
        }


class ReduceTheFloatTests(_PettyCashReturnFixture):
    """Cutting the float banks the cash above it into the fund's own branch's bank."""

    def test_an_exact_count_banks_the_excess_and_lowers_the_float(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA)

        post_petty_cash_return(ret)

        ret.refresh_from_db()
        self.fund.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.POSTED)
        self.assertEqual(self.lines(ret), {
            "1140": (50_000 * NAIRA, 0), "1110": (0, 50_000 * NAIRA),
        })
        self.assertEqual(ret.journal.branch_id, self.ikeja.pk)
        self.assertEqual(self.fund.float_amount, 50_000 * NAIRA)
        self.assertEqual(self.fund.current_balance, 50_000 * NAIRA)
        self.assertTrue(self.fund.is_active)
        audit = FinanceAuditLog.objects.get(
            action=FinanceAuditAction.PETTY_CASH_RETURN_POSTED, target_id=str(ret.pk))
        self.assertEqual(audit.branch_id, self.ikeja.pk)

    def test_a_short_count_posts_the_shortage_to_cash_over_and_short(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=98_000 * NAIRA, amount=48_000 * NAIRA,
            new_float=50_000 * NAIRA, reason="Two notes missing after the PTA meeting")

        post_petty_cash_return(ret)

        ret.refresh_from_db()
        self.fund.refresh_from_db()
        self.assertEqual(self.lines(ret)["1140"], (48_000 * NAIRA, 0))
        self.assertEqual(self.lines(ret)["5530"], (2_000 * NAIRA, 0))
        petty = JournalLine.objects.filter(entry=ret.journal, account__code="1110")
        self.assertEqual(sorted(p.credit for p in petty), [2_000 * NAIRA, 48_000 * NAIRA])
        self.assertEqual(self.fund.current_balance, 50_000 * NAIRA)
        self.assertEqual(ret.shortage, 2_000 * NAIRA)

    def test_an_over_count_credits_cash_over_and_short(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=101_000 * NAIRA, amount=51_000 * NAIRA,
            new_float=50_000 * NAIRA, reason="A refund slip was paid in cash")

        post_petty_cash_return(ret)

        ret.refresh_from_db()
        self.fund.refresh_from_db()
        self.assertEqual(self.lines(ret)["5530"], (0, 1_000 * NAIRA))
        self.assertEqual(self.fund.current_balance, 50_000 * NAIRA)

    def test_a_difference_needs_a_reason(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=98_000 * NAIRA, amount=48_000 * NAIRA,
            new_float=50_000 * NAIRA)

        with self.assertRaisesMessage(PettyCashError, "Say why they differ"):
            post_petty_cash_return(ret)
        self.assertFalse(ret.journal_id)

    def test_another_branchs_bank_is_refused(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA, bank="lekki")

        with self.assertRaisesMessage(PettyCashError, "Ikeja Branch bank account"):
            post_petty_cash_return(ret)
        self.assertEqual(gl_cash_on_hand(self.fund), FLOAT)

    def test_the_tin_may_not_keep_more_than_the_new_float(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=40_000 * NAIRA,
            new_float=50_000 * NAIRA)

        with self.assertRaisesMessage(PettyCashError, "Bank at least"):
            post_petty_cash_return(ret)

    def test_a_count_overtaken_by_a_voucher_is_counted_again(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA)
        self.spend(5_000 * NAIRA)

        with self.assertRaisesMessage(PettyCashError, "Count the tin again"):
            post_petty_cash_return(ret)

    def test_a_closed_period_refuses_the_return(self):
        FiscalPeriod.objects.filter(entity=self.books).update(status=PeriodStatus.CLOSED)
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA)

        with self.assertRaises(PeriodClosedError):
            post_petty_cash_return(ret)
        ret.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.DRAFT)

    def test_the_fund_register_and_the_bank_show_the_return(self):
        from vs_finance.banking import _unmatched_gl_lines
        from vs_finance.views_ops.pettycash import PettyCashFundDetailView

        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=98_000 * NAIRA, amount=48_000 * NAIRA,
            new_float=50_000 * NAIRA, reason="Two notes missing")
        ret = post_petty_cash_return(ret)
        self.fund.refresh_from_db()

        register = PettyCashFundDetailView()._register(self.fund)
        categories = {(row["category"], row["out"]) for row in register}
        self.assertIn(("Returned to bank", 48_000 * NAIRA), categories)
        self.assertIn(("Count short", 2_000 * NAIRA), categories)
        self.assertEqual(register[0]["balance"], 50_000 * NAIRA)
        deposit = [
            line for line in _unmatched_gl_lines(self.ikeja_bank) if line.entry_id == ret.journal_id
        ]
        self.assertEqual([line.debit for line in deposit], [48_000 * NAIRA])


class CloseTheFundTests(_PettyCashReturnFixture):
    """Closing banks the whole tin and stops the fund until it is reopened."""

    def test_closing_banks_the_count_and_stops_the_fund(self):
        old = self.spend(30_000 * NAIRA)
        ret = self.make_return(PettyCashReturnKind.CLOSE, counted=70_000 * NAIRA)
        ret.created_by = self.custodian
        ret.save()

        post_petty_cash_return(ret)

        self.fund.refresh_from_db()
        self.assertEqual(self.lines(ret.__class__.objects.get(pk=ret.pk)), {
            "1140": (70_000 * NAIRA, 0), "1110": (0, 70_000 * NAIRA),
        })
        self.assertTrue(self.fund.is_closed)
        self.assertFalse(self.fund.is_active)
        self.assertEqual(self.fund.closed_on, JAN_10)
        self.assertEqual(self.fund.closed_by, self.custodian)
        self.assertEqual((self.fund.float_amount, self.fund.current_balance), (0, 0))
        closed = FinanceAuditLog.objects.get(action=FinanceAuditAction.PETTY_CASH_FUND_CLOSED)
        self.assertEqual(closed.branch_id, self.ikeja.pk)

        for attempt in (
            lambda: establish_fund(self.fund, bank_account=self.ikeja_bank, amount=1, date=JAN_20),
            lambda: replenish_fund(self.fund, bank_account=self.ikeja_bank, date=JAN_20, amount=1),
            lambda: post_voucher(self.spend(100, post=False)),
            lambda: void_voucher(old),
        ):
            with self.subTest(attempt=attempt), self.assertRaisesMessage(PettyCashError, "Reopen it"):
                attempt()

    def test_a_draft_voucher_blocks_the_closure_until_cancelled(self):
        draft = self.spend(1_000 * NAIRA, post=False)
        ret = self.make_return(PettyCashReturnKind.CLOSE, counted=FLOAT)

        with self.assertRaisesMessage(PettyCashError, "Post or cancel them first"):
            post_petty_cash_return(ret)

        cancel_voucher(draft)
        post_petty_cash_return(ret)
        self.fund.refresh_from_db()
        self.assertTrue(self.fund.is_closed)

    def test_a_closure_that_counts_nothing_writes_off_the_books_without_a_bank(self):
        ret = self.make_return(
            PettyCashReturnKind.CLOSE, counted=0, bank=None, reason="Tin stolen; police report")

        post_petty_cash_return(ret)

        ret.refresh_from_db()
        self.assertEqual(self.lines(ret), {"5530": (FLOAT, 0), "1110": (0, FLOAT)})
        self.fund.refresh_from_db()
        self.assertTrue(self.fund.is_closed)

    def test_reopening_needs_a_reason_and_is_audited(self):
        post_petty_cash_return(self.make_return(PettyCashReturnKind.CLOSE, counted=FLOAT))
        self.fund.refresh_from_db()

        with self.assertRaisesMessage(PettyCashError, "Say why"):
            reopen_fund(self.fund, reason=" ")
        reopen_fund(self.fund, reason="Card terminal withdrawn", float_amount=30_000 * NAIRA)

        self.fund.refresh_from_db()
        self.assertFalse(self.fund.is_closed)
        self.assertTrue(self.fund.is_active)
        self.assertEqual(self.fund.float_amount, 30_000 * NAIRA)
        reopened = FinanceAuditLog.objects.get(action=FinanceAuditAction.PETTY_CASH_FUND_REOPENED)
        self.assertEqual(reopened.branch_id, self.ikeja.pk)
        self.assertEqual(reopened.metadata["reason"], "Card terminal withdrawn")


class VoidAReturnTests(_PettyCashReturnFixture):
    """A return is undone by reversal, until the bank has matched its deposit."""

    def reduce(self):
        ret = self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA)
        return post_petty_cash_return(ret)

    def test_voiding_a_reduction_restores_the_cash_and_the_float(self):
        ret = self.reduce()

        void_petty_cash_return(ret)

        ret.refresh_from_db()
        self.fund.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.REVERSED)
        self.assertEqual(ret.journal.status, DocumentStatus.REVERSED)
        self.assertEqual((self.fund.float_amount, self.fund.current_balance), (FLOAT, FLOAT))

    def test_voiding_a_closure_reopens_the_fund(self):
        ret = post_petty_cash_return(self.make_return(PettyCashReturnKind.CLOSE, counted=FLOAT))

        void_petty_cash_return(ret)

        self.fund.refresh_from_db()
        self.assertFalse(self.fund.is_closed)
        self.assertTrue(self.fund.is_active)
        self.assertEqual(self.fund.float_amount, FLOAT)

    def test_a_matched_deposit_cannot_be_voided(self):
        from vs_finance.banking import import_statement_lines, match_line

        ret = self.reduce()
        _, (statement_line,), _ = import_statement_lines(
            self.ikeja_bank, [{"txn_date": JAN_10, "amount": 50_000 * NAIRA, "external_id": "D1"}])
        match_line(statement_line, ret.journal.lines.get(account=self.ikeja_bank.gl_account))

        with self.assertRaisesMessage(PettyCashError, "Unmatch it on the reconciliation"):
            void_petty_cash_return(ret)
        ret.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.POSTED)

    def test_the_earlier_of_two_returns_waits_for_the_later(self):
        first = self.reduce()
        second = post_petty_cash_return(self.make_return(
            PettyCashReturnKind.REDUCE, counted=50_000 * NAIRA, amount=20_000 * NAIRA,
            new_float=30_000 * NAIRA))

        with self.assertRaisesMessage(PettyCashError, "Void it first"):
            void_petty_cash_return(first)
        void_petty_cash_return(second)
        void_petty_cash_return(first)

    def test_the_journal_is_reversed_only_through_the_return(self):
        from vs_finance.posting import journal_reversal_action

        ret = self.reduce()

        action = journal_reversal_action(ret.journal)
        self.assertEqual(action["kind"], "VOID_DOCUMENT")
        self.assertEqual(action["document_type"], "PETTY_CASH_RETURN")


class FundEditTests(_PettyCashReturnFixture):
    """A plain edit moves no cash, so it refuses what only a return may do."""

    def test_lowering_the_float_below_the_cash_is_a_reduction(self):
        with self.assertRaisesMessage(PettyCashError, "Reduce the float with a return"):
            change_fund_details(self.fund, float_amount=50_000 * NAIRA)

    def test_deactivating_a_fund_holding_cash_is_a_closure(self):
        with self.assertRaisesMessage(PettyCashError, "Close the fund instead"):
            change_fund_details(self.fund, is_active=False)

    def test_a_custodian_change_is_audited(self):
        successor = self.user_for(self.tenant, "okafor@corona.test")

        change_fund_details(self.fund, custodian_id=successor.pk)

        entry = FinanceAuditLog.objects.get(action=FinanceAuditAction.PETTY_CASH_FUND_UPDATED)
        self.assertEqual(entry.before, {"custodian_id": self.custodian.pk})
        self.assertEqual(entry.after, {"custodian_id": successor.pk})
        self.assertEqual(entry.branch_id, self.ikeja.pk)


class PettyCashReturnApiTests(_PettyCashReturnFixture):
    """The routes: keys, branch reach, the tenant boundary, and approval."""

    def officer(self, *keys, branch=None, tenant=None, tag=""):
        tenant = tenant or self.tenant
        user = self.grant(
            self.user_for(tenant, f"officer{tag}-{len(keys)}-{branch and branch.pk}@corona.test"),
            "finance.pettycash.view", *keys,
            tenant=tenant, role_key=f"pc-{tag}-{len(keys)}-{branch and branch.pk}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def call(self, client, path, body, *, entity=None):
        entity = entity or self.books
        return client.post(f"/v1/finance/{path}?entity={entity.code}", body, format="json")

    def reduce_body(self, **extra):
        return {
            "counted_amount": FLOAT, "new_float_amount": 50_000 * NAIRA,
            "bank_account": self.ikeja_bank.pk, "return_date": JAN_10.isoformat(), **extra,
        }

    def test_without_the_key_the_route_is_refused(self):
        client = self.officer(branch=self.ikeja, tag="nokey")

        response = self.call(client, f"petty-cash-funds/{self.fund.pk}/reduce/", self.reduce_body())

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PettyCashReturn.objects.exists())

    def test_the_close_key_does_not_reduce_and_the_return_key_does_not_close(self):
        reducer = self.officer("finance.pettycash.return", branch=self.ikeja, tag="r")
        response = self.call(reducer, f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_10.isoformat(),
        })
        self.assertEqual(response.status_code, 403)

    def test_an_ikeja_officer_reduces_ikejas_float(self):
        client = self.officer("finance.pettycash.return", branch=self.ikeja, tag="ok")

        response = self.call(client, f"petty-cash-funds/{self.fund.pk}/reduce/", self.reduce_body())

        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(data["status"], DocumentStatus.POSTED)
        self.assertEqual(data["amount"], 50_000 * NAIRA)
        self.assertEqual(data["branch_id"], self.ikeja.pk)
        self.assertEqual(data["counted_by_id"], self.custodian.pk)
        self.assertEqual(data["fund"]["float_amount"], 50_000 * NAIRA)

    def test_an_ikeja_officer_cannot_reach_lekkis_fund_or_bank(self):
        client = self.officer("finance.pettycash.return", branch=self.ikeja, tag="reach")

        lekki_fund = self.call(
            client, f"petty-cash-funds/{self.lekki_fund.pk}/reduce/",
            self.reduce_body(new_float_amount=10_000 * NAIRA, counted_amount=20_000 * NAIRA))
        lekki_bank = self.call(
            client, f"petty-cash-funds/{self.fund.pk}/reduce/",
            self.reduce_body(bank_account=self.lekki_bank.pk))

        self.assertEqual(lekki_fund.status_code, 404)
        self.assertEqual(lekki_bank.status_code, 404)
        self.assertFalse(PettyCashReturn.objects.exists())

    def test_a_whole_tenant_officer_still_banks_only_into_the_funds_branch(self):
        client = self.officer("finance.pettycash.return", tag="whole")

        response = self.call(
            client, f"petty-cash-funds/{self.fund.pk}/reduce/",
            self.reduce_body(bank_account=self.lekki_bank.pk))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("Ikeja Branch", str(response.data))

    def test_another_tenant_cannot_see_the_fund(self):
        client = self.officer(
            "finance.pettycash.return", branch=self.rival_branch, tenant=self.rival_tenant,
            tag="rival")

        response = self.call(client, f"petty-cash-funds/{self.fund.pk}/reduce/", self.reduce_body())

        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(PettyCashReturn.objects.exists())

    def test_a_difference_without_a_reason_is_refused_on_the_field(self):
        client = self.officer("finance.pettycash.return", branch=self.ikeja, tag="why")

        response = self.call(
            client, f"petty-cash-funds/{self.fund.pk}/reduce/",
            self.reduce_body(counted_amount=98_000 * NAIRA))

        self.assertEqual(response.status_code, 400)
        self.assertIn("difference_reason", str(response.data))
        self.assertFalse(PettyCashReturn.objects.exists())

    def test_closing_then_no_voucher_and_the_list_shows_only_own_branch(self):
        client = self.officer(
            "finance.pettycash.close", "finance.pettycashvoucher.create",
            branch=self.ikeja, tag="close")

        closed = self.call(client, f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_10.isoformat(),
        })
        self.assertEqual(closed.status_code, 201, closed.data)
        self.assertEqual(closed.data["data"]["fund"]["state"], "CLOSED")

        voucher = self.call(client, "petty-cash-vouchers/", {
            "fund": self.fund.pk, "voucher_date": JAN_20.isoformat(),
            "lines": [{"expense_account": "5300", "unit_price": 100}],
        })
        self.assertEqual(voucher.status_code, 400)

        whole = self.officer("finance.pettycash.return", tag="lekkiret")
        self.call(whole, f"petty-cash-funds/{self.lekki_fund.pk}/reduce/", {
            "counted_amount": 20_000 * NAIRA, "new_float_amount": 10_000 * NAIRA,
            "bank_account": self.lekki_bank.pk, "return_date": JAN_10.isoformat(),
        })
        listed = client.get(f"/v1/finance/petty-cash-returns/?entity={self.books.code}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual({row["fund_id"] for row in listed.data["data"]}, {self.fund.pk})

    def test_a_route_holds_a_closure_for_approval_and_posts_it_once_approved(self):
        from vs_finance.workflow_handlers import PettyCashReturnHandler
        from vs_workflow.models import WorkflowStage, WorkflowTemplate

        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type="finance.petty_cash_return",
            code="standard", name="Petty cash returns")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Closure approval", order=10,
            approver_role_key="finance-approver",
            inclusion_condition={"op": "eq", "field": "kind", "value": "CLOSE"})
        client = self.officer(
            "finance.pettycash.close", "finance.pettycash.return", branch=self.ikeja, tag="wf")

        reduced = self.call(client, f"petty-cash-funds/{self.fund.pk}/reduce/", self.reduce_body())
        self.assertEqual(reduced.data["data"]["status"], DocumentStatus.POSTED)

        held = self.call(client, f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": 50_000 * NAIRA, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_20.isoformat(),
        })
        self.assertEqual(held.status_code, 201, held.data)
        self.assertEqual(held.data["data"]["status"], DocumentStatus.PENDING_APPROVAL)
        self.assertIn("approval", held.data["data"])
        ret = PettyCashReturn.objects.get(pk=held.data["data"]["id"])
        self.assertIsNone(ret.journal_id)
        self.fund.refresh_from_db()
        self.assertFalse(self.fund.is_closed)

        ret.status = DocumentStatus.APPROVED
        ret.save(update_fields=["status"])
        PettyCashReturnHandler().post(ret, actor_user=None)
        self.fund.refresh_from_db()
        self.assertTrue(self.fund.is_closed)

    def test_a_closure_whose_approval_ends_unapproved_is_cancelled_and_counted_again(self):
        """Mrs Adeyemi's count is a snapshot: once its approval ends, she counts afresh.

        Rejected by the approver, withdrawn by the officer who raised it or cancelled
        by an administrator, the closure is CANCELLED rather than a draft nobody can
        send, and a new closure with a new count goes through the same route.
        """
        from vs_rbac.models import TenantRoleTemplate
        from vs_workflow.constants import WorkflowStageAction
        from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
        from vs_workflow.services.actions import cancel, record_action, withdraw

        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type="finance.petty_cash_return",
            code="standard", name="Petty cash returns")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Closure approval", order=10,
            approver_role_key="finance-approver")
        approver = self.grant(self.user_for(self.tenant, "closure-approver@corona.test"),
                              tenant=self.tenant, role_key="finance-approver")
        TenantRoleTemplate.objects.filter(tenant=self.tenant, key="finance-approver").update(
            is_system_role=True)
        client = self.officer("finance.pettycash.close", branch=self.ikeja, tag="ended")
        body = {"counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
                "return_date": JAN_20.isoformat()}

        for how in ("rejected", "withdrawn", "cancelled"):
            with self.subTest(how=how):
                held = self.call(client, f"petty-cash-funds/{self.fund.pk}/close/", body)
                self.assertEqual(held.status_code, 201, held.data)
                pk = held.data["data"]["id"]
                instance = WorkflowInstance.all_objects.get(
                    document_object_id=str(pk), document_type="finance.petty_cash_return")
                if how == "rejected":
                    record_action(instance.id, approver, WorkflowStageAction.REJECTED,
                                  comment="Recount it")
                elif how == "withdrawn":
                    withdraw(instance.id, instance.requested_by)
                else:
                    cancel(instance.id, approver, "Raised in error")

                ret = PettyCashReturn.objects.get(pk=pk)
                self.assertEqual(ret.status, DocumentStatus.CANCELLED)
                self.assertIsNone(ret.journal_id)
                self.fund.refresh_from_db()
                self.assertFalse(self.fund.is_closed)

    def test_a_returned_closure_resumed_from_approvals_waits_for_approval(self):
        """Returned to the officer and resumed from her approvals, the closure waits again."""
        from vs_rbac.models import TenantRoleTemplate
        from vs_workflow.constants import WorkflowStageAction
        from vs_workflow.models import WorkflowInstance, WorkflowStage, WorkflowTemplate
        from vs_workflow.services.actions import record_action

        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type="finance.petty_cash_return",
            code="standard", name="Petty cash returns")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Closure approval", order=10,
            approver_role_key="finance-approver")
        approver = self.grant(self.user_for(self.tenant, "resume-approver@corona.test"),
                              tenant=self.tenant, role_key="finance-approver")
        TenantRoleTemplate.objects.filter(tenant=self.tenant, key="finance-approver").update(
            is_system_role=True)
        client = self.officer("finance.pettycash.close", branch=self.ikeja, tag="resume")
        held = self.call(client, f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_20.isoformat(),
        })
        pk = held.data["data"]["id"]
        instance = WorkflowInstance.all_objects.get(
            document_object_id=str(pk), document_type="finance.petty_cash_return")
        record_action(instance.id, approver, WorkflowStageAction.RETURNED, comment="Recount")
        self.assertEqual(PettyCashReturn.objects.get(pk=pk).status, DocumentStatus.DRAFT)

        resumed = client.post(f"/v1/workflow/instances/{instance.id}/resubmit/", {}, format="json")

        self.assertEqual(resumed.status_code, 200, resumed.data)
        self.assertEqual(PettyCashReturn.objects.get(pk=pk).status, DocumentStatus.PENDING_APPROVAL)
        again = self.call(client, f"petty-cash-funds/{self.fund.pk}/close/", {
            "counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
            "return_date": JAN_20.isoformat(),
        })
        self.assertNotEqual(again.status_code, 201, again.data)

    def test_voiding_needs_the_reverse_key(self):
        post_petty_cash_return(self.make_return(
            PettyCashReturnKind.REDUCE, counted=FLOAT, amount=50_000 * NAIRA,
            new_float=50_000 * NAIRA))
        ret = PettyCashReturn.objects.get()
        without = self.officer("finance.pettycash.return", branch=self.ikeja, tag="nov")
        allowed = self.officer("finance.pettycash.reverse", branch=self.ikeja, tag="rev")

        self.assertEqual(self.call(without, f"petty-cash-returns/{ret.pk}/void/", {}).status_code, 403)
        voided = self.call(allowed, f"petty-cash-returns/{ret.pk}/void/", {})
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(voided.data["data"]["status"], DocumentStatus.REVERSED)


class OneBranchSchoolTests(_FinanceBranchFixture):
    """At a school with one branch the fund and its bank need no branch named."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        e = cls.solo_books
        cls.bank = _bank(e, "Main Operations", None, "50")
        cls.fund = PettyCashFund.objects.create(
            entity=e, name="Front desk", float_amount=FLOAT,
            gl_account=Account.objects.get(entity=e, code="1110"),
        )
        establish_fund(cls.fund, bank_account=cls.bank, amount=FLOAT, date=JAN_2)

    def test_a_bursar_reduces_the_float_without_naming_a_branch(self):
        user = self.grant(
            self.user_for(self.solo_tenant, "bursar@solo.test"),
            "finance.pettycash.view", "finance.pettycash.return",
            tenant=self.solo_tenant, role_key="solo-bursar", branch=self.solo_main,
        )
        response = TenantAPIClient(user=user).post(
            f"/v1/finance/petty-cash-funds/{self.fund.pk}/reduce/?entity={self.solo_books.code}",
            {"counted_amount": FLOAT, "new_float_amount": 40_000 * NAIRA,
             "bank_account": self.bank.pk, "return_date": JAN_10.isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["branch_id"], self.solo_main.pk)
        self.assertEqual(response.data["data"]["amount"], 60_000 * NAIRA)
