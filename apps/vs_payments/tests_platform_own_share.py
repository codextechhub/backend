"""The platform pays its own staff and suppliers only from its own share of the provider balance.

CodeX's money and the money it holds for schools sit in one Paystack balance. With
N3,000,000 in it and N2,600,000 held for schools, at most N400,000 may leave for
CodeX's own payouts: a N1,200,000 CodeX payroll waits for funds, and its bursar can
record it as paid from the bank instead. Every transfer already reserved and not yet
paid or failed counts against the share, whatever it pays and whichever tenant it is
for. A school's own settlement or supplier payment is checked against that branch's
held balance and never against the platform's share, so a CodeX payroll already
reserved cannot hold up a school. A provider that cannot report its balance means
nothing is sent.

Here Lekki holds N1,780 and Ikeja N1,000 (N2,780 for schools) and the balance is set
to N3,780 or less around it.
"""
from __future__ import annotations


import threading
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, tag

from vs_config.clock import tenant_today
from vs_finance.constants import PayrollLinePayState as State
from vs_finance.constants import PayrollRunStatus, StaffBankState
from vs_finance.models import (
    Account,
    EmployeeSalary,
    FinancePayrollSettings,
    JournalLine,
    StaffBankAccount,
)
from vs_finance.payroll import generate_run_from_roster, post_payroll, submit_payroll_run
from vs_finance.payroll_settings import update_finance_payroll_settings
from vs_procurement.models import Vendor

from . import held, payroll_payouts, services
from .constants import PayoutBatchStatus, PayoutPurpose, PayoutStatus
from .exceptions import ProviderError
from .models import PayoutBatch, PayoutInstruction, PaymentCustodySettings
from .providers import registry
from .providers.fake import FakeProvider
from .tests_payroll_payouts import FEE, _PayrollFixture


class _PlatformFixture(_PayrollFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.codex = cls.platform.tenant
        FinancePayrollSettings.objects.create(
            entity=cls.platform, paye_method="SUPPLIED", employer_pension_enabled=False,
            nhf_enabled=False, nsitf_enabled=False, itf_enabled=False,
            payment_route="PAYSTACK")
        cls.codex_user, cls.codex_salary, cls.codex_bank = cls.staff(
            cls.platform, cls.codex, cls.lagos, "tola@codex.test", "Tola", "Adamu",
            gross=60_000, paye=0, pension=0, number="0123456790")
        cls.codex_operator = cls.user_for(cls.codex, "payroll@codex.test")
        cls.codex_approver = cls.grant(
            cls.user_for(cls.codex, "payroll-approver@codex.test"), "finance.payrollrun.post",
            tenant=cls.codex, role_key="payroll-approver")

    def platform_run(self):
        run = generate_run_from_roster(
            self.platform, pay_date=tenant_today(self.codex), period_label="January",
            actor_user=self.codex_operator)
        submit_payroll_run(run, requested_by=self.codex_operator)
        self.approve_run(run, self.codex_approver)
        run.refresh_from_db()
        post_payroll(run, actor_user=self.codex_approver)
        run.refresh_from_db()
        return run

    def schools_hold(self):
        """N1,780 for Lekki and N1,000 for Ikeja are held for schools."""
        self.paid(self.books, self.adeyemi, 178_000, fee=0)
        self.paid(self.books, self.okafor, 100_000, fee=0)
        return held.held_balance(self.lekki.pk) + held.held_balance(self.ikeja.pk)

    def provider_holds(self, own):
        self.fake.balances["NGN"] = self.schools_held_total + own

    def prepare(self, own):
        self.schools_held_total = self.schools_hold()
        self.provider_holds(own)


class PlatformPayrollOwnShareTests(_PlatformFixture):

    def test_a_payroll_within_the_platforms_own_share_is_sent_and_touches_no_schools_money(self):
        self.prepare(own=100_000)
        before = (held.held_balance(self.lekki.pk), held.held_balance(self.ikeja.pk))
        run = self.platform_run()
        payroll_payouts.pay_run(run, actor_user=self.codex_operator)
        batch = PayoutBatch.objects.get(purpose=PayoutPurpose.PAYROLL, entity=self.platform)
        payroll_payouts.dispatch_payroll_batch(batch.pk)

        payout = batch.instructions.get()
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(payout.metadata["own_share_reserved"], 60_000 + FEE)
        self.assertEqual((held.held_balance(self.lekki.pk), held.held_balance(self.ikeja.pk)), before)
        self.assertEqual(held.own_share_available(self.fake), 100_000 - 60_000 - FEE)

    def test_a_paid_platform_line_books_against_clearing_and_the_run_reads_paid(self):
        self.prepare(own=100_000)
        run = self.platform_run()
        payroll_payouts.pay_run(run, actor_user=self.codex_operator)
        batch = PayoutBatch.objects.get(purpose=PayoutPurpose.PAYROLL, entity=self.platform)
        payroll_payouts.dispatch_payroll_batch(batch.pk)
        payout = self.confirm(batch.instructions.get())

        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        wages = JournalLine.objects.get(
            entry__entity=self.platform, account__code="2330", debit=60_000)
        self.assertEqual(wages.entry.branch_id, self.lagos.pk)
        self.assertEqual(JournalLine.objects.get(entry=wages.entry, account__code="1125").credit, 60_000)
        fee = JournalLine.objects.get(entry__entity=self.platform, account__code="5500")
        self.assertEqual(fee.debit, FEE)
        from . import held_reconciliation

        self.assertEqual(held_reconciliation.own_paid_out(self.platform), 60_000 + FEE)
        self.assertEqual(payout.status, PayoutStatus.PAID)

    def test_a_payroll_larger_than_the_share_waits_and_is_sent_when_the_share_grows(self):
        self.prepare(own=20_000)
        run = self.platform_run()
        payroll_payouts.pay_run(run, actor_user=self.codex_operator)
        batch = PayoutBatch.objects.get(purpose=PayoutPurpose.PAYROLL, entity=self.platform)
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            payroll_payouts.dispatch_payroll_batch(batch.pk)
        sent.assert_not_called()
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.WAITING_FOR_FUNDS)
        line = run.lines.get()
        self.assertEqual(line.pay_state, State.WAITING_FOR_FUNDS)
        self.assertIn("own share", line.pay_note)

        self.provider_holds(own=100_000)
        payroll_payouts.sweep_payroll_batches()
        self.assertEqual(batch.instructions.get().status, PayoutStatus.PROCESSING)

    def test_a_provider_that_cannot_report_its_balance_sends_nothing(self):
        self.prepare(own=100_000)
        run = self.platform_run()
        payroll_payouts.pay_run(run, actor_user=self.codex_operator)
        batch = PayoutBatch.objects.get(purpose=PayoutPurpose.PAYROLL, entity=self.platform)
        down = ProviderError("Paystack is down.", provider="PAYSTACK", provider_code="503")
        with patch.object(self.fake, "available_balance", side_effect=down):
            with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
                payroll_payouts.dispatch_payroll_batch(batch.pk)
        sent.assert_not_called()
        self.assertEqual(run.lines.get().pay_state, State.WAITING_FOR_FUNDS)
        self.assertIn("could not be confirmed", run.lines.get().pay_note)
        self.assertEqual(batch.instructions.get().status, PayoutStatus.PENDING)

    def test_the_reservation_itself_refuses_when_the_balance_cannot_be_read(self):
        payout = PayoutInstruction(entity=self.platform, provider="PAYSTACK", amount=1_000)
        with patch.object(self.fake, "available_balance", side_effect=NotImplementedError("none")):
            with self.assertRaises(held.OwnShareUnknownError):
                held.reserve_for_transfer(payout, 1_000, fee=FEE, client=self.fake)
        self.assertNotIn("own_share_reserved", payout.metadata or {})

    def test_the_platform_chooses_the_route_whatever_its_custody_says(self):
        FinancePayrollSettings.objects.filter(entity=self.platform).update(payment_route="OWN_BANK")
        PaymentCustodySettings.objects.update_or_create(
            tenant=self.codex, defaults={"mode": "DIRECT"})
        settings = update_finance_payroll_settings(
            entity=self.platform, data={"payment_route": "PAYSTACK"},
            actor_user=self.codex_operator)
        self.assertEqual(settings.payment_route, "PAYSTACK")
        payroll_payouts.assert_route_allows(self.platform)


class ReservationAccountingTests(_PlatformFixture):
    """Every reservation in flight, whatever it is for, counts against the platform's share."""

    def own_payout(self):
        return PayoutInstruction.objects.create(
            entity=self.platform, provider="PAYSTACK", reference=f"T-{PayoutInstruction.objects.count()}",
            amount=60_000, beneficiary_name="X", beneficiary_account_number="0123456790",
            beneficiary_bank_code="058", status=PayoutStatus.PENDING)

    def test_a_second_platform_payout_sees_the_first_ones_reservation(self):
        self.prepare(own=100_000)
        first, second = self.own_payout(), self.own_payout()
        held._reserve_own_share(first, 60_000, FEE, self.fake)
        first.status = PayoutStatus.PROCESSING
        first.save()
        with self.assertRaises(held.InsufficientHeldFundsError) as raised:
            held._reserve_own_share(second, 60_000, FEE, self.fake)
        self.assertIn("own share", str(raised.exception.message))
        self.assertEqual(held.own_share_available(self.fake), 100_000 - 60_000 - FEE)

    def test_a_failed_reservation_is_given_back_to_the_share(self):
        self.prepare(own=100_000)
        first = self.own_payout()
        held._reserve_own_share(first, 60_000, FEE, self.fake)
        first.status = PayoutStatus.PROCESSING
        first.save()
        first.status = PayoutStatus.FAILED
        first.save()
        self.assertEqual(held.own_share_available(self.fake), 100_000)

    def test_a_schools_transfer_in_flight_does_not_eat_into_the_platforms_share(self):
        self.prepare(own=100_000)
        school = self.dispatch(self.vendor_payout(10_000, self.lekki_bank))
        self.assertEqual(school.status, PayoutStatus.PROCESSING)
        # The provider has not reported the transfer yet, so its balance is unchanged; the
        # branch's held balance fell by the transfer and its fee, and that same amount in
        # flight is subtracted once, so the platform's share is exactly what it was.
        self.assertEqual(held.own_share_available(self.fake), 100_000)

    def test_a_schools_settlement_is_never_blocked_by_a_platform_reservation(self):
        self.prepare(own=100_000)
        run = self.platform_run()
        payroll_payouts.pay_run(run, actor_user=self.codex_operator)
        batch = PayoutBatch.objects.get(purpose=PayoutPurpose.PAYROLL, entity=self.platform)
        payroll_payouts.dispatch_payroll_batch(batch.pk)
        self.assertEqual(batch.instructions.get().status, PayoutStatus.PROCESSING)

        intent = self.paid(self.books, self.adeyemi, 50_000, fee=0)
        self.yesterday(intent)
        settlement, _ = held.build_settlement(self.books, self.lekki.pk)
        payout = self.confirm(self.dispatch(settlement.batch), amount=settlement.amount)
        self.assertEqual(payout.status, PayoutStatus.PAID)
        school = self.dispatch(self.vendor_payout(10_000, self.ikeja_bank))
        self.assertEqual(school.status, PayoutStatus.PROCESSING)

    def test_a_platform_supplier_is_paid_from_the_share_and_booked_against_clearing(self):
        self.prepare(own=100_000)
        vendor = Vendor.objects.create(
            entity=self.platform, code="CXV1", name="Lagos Cloud Ltd",
            payable_account=Account.objects.get(entity=self.platform, code="2100"),
            default_expense_account=Account.objects.get(entity=self.platform, code="5300"),
            bank_name="GTBank", bank_code="058", bank_account_name="Lagos Cloud Ltd",
            bank_account_number="0123456791", kyc_status="VERIFIED")
        batch = services.create_payout_batch(
            entity=self.platform, items=[{"amount": 50_000, "vendor": vendor}])
        payout = self.dispatch(batch)
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(payout.metadata["own_share_reserved"], 50_000 + FEE)
        self.confirm(payout)
        credit = JournalLine.objects.get(
            entry__entity=self.platform, account__code="1125", credit=50_000)
        self.assertEqual(credit.entry.branch_id, self.lagos.pk)
        self.assertEqual(
            JournalLine.objects.get(entry__entity=self.platform, account__code="5500").debit, FEE)

    def test_a_platform_supplier_larger_than_the_share_is_refused_and_sends_nothing(self):
        self.prepare(own=10_000)
        vendor = Vendor.objects.create(
            entity=self.platform, code="CXV2", name="Lagos Cloud Ltd",
            payable_account=Account.objects.get(entity=self.platform, code="2100"),
            default_expense_account=Account.objects.get(entity=self.platform, code="5300"),
            bank_name="GTBank", bank_code="058", bank_account_name="Lagos Cloud Ltd",
            bank_account_number="0123456791", kyc_status="VERIFIED")
        batch = services.create_payout_batch(
            entity=self.platform, items=[{"amount": 50_000, "vendor": vendor}])
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            payout = self.dispatch(batch)
        sent.assert_not_called()
        self.assertEqual(payout.status, PayoutStatus.FAILED)
        self.assertIn("own share", payout.failure_reason)


@tag("slow")
class ConcurrentReservationTests(TransactionTestCase):
    """Two platform payouts racing for the same money cannot both pass the check."""

    serialized_rollback = True

    def setUp(self):
        self.fake = FakeProvider(secret="test-secret")
        self.fake.balances["NGN"] = 100_000
        registry.register("PAYSTACK", self.fake)
        self.addCleanup(registry.unregister, "PAYSTACK")
        self.platform = held.platform_books()
        self.payouts = [
            PayoutInstruction.objects.create(
                entity=self.platform, provider="PAYSTACK", reference=f"RACE-{n}", amount=60_000,
                beneficiary_name="X", beneficiary_account_number="0123456790",
                beneficiary_bank_code="058", status=PayoutStatus.PENDING)
            for n in (1, 2)
        ]

    def test_the_second_waits_for_the_first_and_is_then_refused(self):
        first_reserved, first_may_commit = threading.Event(), threading.Event()
        outcome = {}

        def first():
            try:
                with transaction.atomic():
                    held._reserve_own_share(self.payouts[0], 60_000, 1_000, self.fake)
                    first_reserved.set()
                    first_may_commit.wait(10)
                    self.payouts[0].status = PayoutStatus.PROCESSING
                    self.payouts[0].save()
                outcome["first"] = "passed"
            finally:
                connections.close_all()

        def second():
            try:
                first_reserved.wait(10)
                with transaction.atomic():
                    held._reserve_own_share(self.payouts[1], 60_000, 1_000, self.fake)
                outcome["second"] = "passed"
            except held.InsufficientHeldFundsError:
                outcome["second"] = "refused"
            finally:
                connections.close_all()

        one, two = threading.Thread(target=first), threading.Thread(target=second)
        one.start()
        two.start()
        two.join(1.0)
        self.assertTrue(two.is_alive(), "The second reservation did not wait for the lock.")
        first_may_commit.set()
        one.join(10)
        two.join(10)
        self.assertEqual(outcome, {"first": "passed", "second": "refused"})
