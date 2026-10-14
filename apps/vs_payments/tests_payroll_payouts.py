"""A posted payroll run is paid by transfer from the money the platform holds for each branch.

Corona runs one January payroll for Ikeja and Lekki. Ada Obi (Ikeja, net N450.00)
and Bola Lawal (Lekki, net N720.00) are paid from what the platform holds for
their own branch, a transfer fee of N10 each on top, borne by the branch. The run's
approval and posting are the authority: no payout approval is asked for. If Lekki
holds less than its batch needs, Lekki's batch waits for funds while Ikeja's staff
are paid. A member of staff whose bank account has not been verified is not sent
anything; their line says so, and they can be paid later or recorded as paid from
the bank. Single Site, with one branch, pays through the same route with one batch.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.constants import PayrollLinePayState as State
from vs_finance.constants import PayrollRunStatus, StaffBankState
from vs_finance.models import (
    BankAccount,
    EmployeeSalary,
    FinancePayrollSettings,
    JournalLine,
    Payslip,
    PayrollLine,
    PayrollRun,
    PayrollRunBranch,
    StaffBankAccount,
)
from vs_finance.staff_banking import confirm_staff_bank_account
from vs_workflow.models import WorkflowInstance

from . import custody, held, held_reconciliation, payroll_payouts, services
from .constants import (
    CustodyMode,
    HeldMovementKind,
    PayoutBatchStatus,
    PayoutPurpose,
    PayoutStatus,
)
from .exceptions import PaymentStateError, PayoutApprovalRequiredError
from .models import HeldMovement, PayoutBatch, PayoutInstruction, PaymentCustodySettings
from .tests_custody_held import _HeldFixture

FEE = 1_000
PAY_KEYS = (
    "finance.payrollrun.create", "finance.payrollrun.view", "finance.payrollrun.post",
    "finance.payrollrun.pay", "finance.payrollrun.submit", "finance.settings.view", "finance.settings.update",
    "payments.settings.update", "payments.settings.view",
)


class _PayrollFixture(_HeldFixture):

    @classmethod
    def person(cls, tenant, email, first, last):
        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=tenant, branch=None, status="ACTIVE",
            first_name=first, last_name=last)

    @classmethod
    def staff(cls, books, tenant, branch, email, first, last, *, gross, paye, pension,
              number, state=StaffBankState.VERIFIED):
        user = cls.person(tenant, email, first, last)
        salary = EmployeeSalary.objects.create(
            entity=books, name=f"{first} {last}", branch=branch, employee=user,
            gross_amount=gross, paye_amount=paye, pension_amount=pension)
        account = StaffBankAccount.objects.create(
            entity=books, employee=user, bank_code="058", bank_name="GTBank",
            account_number=number, resolved_account_name=f"{last} {first}".upper(),
            verification_state=state, changed_by=cls.person(
                tenant, f"maker-{email}", "Maker", "Person"))
        return user, salary, account

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for books in (cls.books, cls.solo_books):
            FinancePayrollSettings.objects.create(
                entity=books, paye_method="SUPPLIED", employer_pension_enabled=False,
                nhf_enabled=False, nsitf_enabled=False, itf_enabled=False,
                payment_route="PAYSTACK")
        cls.ada, cls.ada_salary, cls.ada_bank = cls.staff(
            cls.books, cls.tenant, cls.ikeja, "ada@corona.test", "Ada", "Obi",
            gross=50_000, paye=3_000, pension=2_000, number="0123456789")
        cls.bola, cls.bola_salary, cls.bola_bank = cls.staff(
            cls.books, cls.tenant, cls.lekki, "bola@corona.test", "Bola", "Lawal",
            gross=80_000, paye=5_000, pension=3_000, number="0123456780")
        cls.sade, cls.sade_salary, cls.sade_bank = cls.staff(
            cls.solo_books, cls.solo_tenant, cls.solo_main, "sade@solo.test", "Sade", "Bello",
            gross=60_000, paye=4_000, pension=2_000, number="0123456781")
        cls.bursar_user = cls.grant(
            cls.user_for(cls.tenant, "payroll@corona.test"), *PAY_KEYS,
            tenant=cls.tenant, role_key="payroll-bursar")
        cls.solo_bursar_user = cls.grant(
            cls.user_for(cls.solo_tenant, "payroll@solo.test"), *PAY_KEYS,
            tenant=cls.solo_tenant, role_key="payroll-bursar")
        cls.checker = cls.user_for(cls.tenant, "checker@corona.test")
        # The second person who approves a run: the route's group starts with the
        # roles holding the post key, and the preparer is left off every decision.
        cls.approver = cls.grant(
            cls.user_for(cls.tenant, "payroll-approver@corona.test"), "finance.payrollrun.post",
            tenant=cls.tenant, role_key="payroll-approver")
        cls.solo_approver = cls.grant(
            cls.user_for(cls.solo_tenant, "payroll-approver@solo.test"), "finance.payrollrun.post",
            tenant=cls.solo_tenant, role_key="payroll-approver")

    def setUp(self):
        super().setUp()
        self.fake.transfer_fee_kobo = FEE
        self.bursar = TenantAPIClient(user=self.bursar_user)
        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        notify.start()
        self.addCleanup(notify.stop)

    # -- helpers --------------------------------------------------------------- #

    @classmethod
    def ops_bank(cls, entity, name, branch, tag):
        from vs_finance.models import Account

        gl = Account.objects.create(
            entity=entity, code=f"19{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=entity, code="1000").account_type,
            is_postable=True)
        return BankAccount.objects.create(entity=entity, name=name, branch=branch, gl_account=gl)

    def fund(self, customer, amount, books=None):
        """Online money held for the customer's branch, with no provider fee on the payment."""
        return self.paid(books or self.books, customer, amount, fee=0)

    def act(self, run, action, body=None, client=None, books=None):
        books = books or self.books
        return (client or self.bursar).post(
            f"/v1/finance/payroll-runs/{run.pk}/{action}/?entity={books.code}",
            body or {}, format="json")

    def generated_run(self, client=None, books=None):
        books = books or self.books
        client = client or self.bursar
        today = tenant_today(books.tenant).isoformat()
        response = client.post(
            f"/v1/finance/payroll-runs/generate/?entity={books.code}",
            {"pay_date": today, "period_label": "January"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return PayrollRun.objects.get(pk=response.data["data"]["id"])

    def approve_run(self, run, approver):
        """The approver decides the run's open request, as the approvals screen does."""
        from vs_workflow.constants import WorkflowStageAction
        from vs_workflow.services import actions as wf_actions

        instance = WorkflowInstance.all_objects.for_document(run).get()
        wf_actions.record_action(instance.id, approver, WorkflowStageAction.APPROVED)

    def posted_run(self, client=None, books=None, approver=None):
        """A run prepared and sent by the bursar, approved by a second person, and posted."""
        books = books or self.books
        client = client or self.bursar
        run = self.generated_run(client, books)
        sent = self.act(run, "submit", client=client, books=books)
        self.assertEqual(sent.status_code, 200, sent.data)
        self.approve_run(run, approver or (
            self.approver if books == self.books else self.solo_approver))
        posted = self.act(run, "post", client=client, books=books)
        self.assertEqual(posted.status_code, 200, posted.data)
        run.refresh_from_db()
        return run

    def pay(self, run, client=None, books=None, expect=200):
        response = self.act(run, "pay", client=client, books=books)
        self.assertEqual(response.status_code, expect, response.data)
        return response

    def batches(self, run):
        return PayoutBatch.objects.filter(
            purpose=PayoutPurpose.PAYROLL, metadata__payroll_run_id=run.pk).order_by("pk")

    def batch_for(self, run, branch):
        return self.batches(run).get(branch=branch)

    def send(self, run):
        """Dispatch every batch of the run, as the worker does after the request commits."""
        for batch in self.batches(run):
            payroll_payouts.dispatch_payroll_batch(batch.pk)

    def line(self, run, user):
        return PayrollLine.objects.get(run=run, employee=user)

    def payout_of(self, run, user):
        return PayoutInstruction.objects.filter(
            metadata__payroll_line_id=self.line(run, user).pk).order_by("-pk").first()

    def refresh(self, *rows):
        for row in rows:
            row.refresh_from_db()


class PayrollRouteTests(_PayrollFixture):
    """Paying a run through the provider creates a batch per branch, sent without a payout approval."""

    def test_paying_makes_one_batch_per_branch_and_asks_for_no_payout_approval(self):
        run = self.posted_run()
        response = self.pay(run)
        self.assertEqual(response.data["data"]["payments"]["counts"]["sent"], 2)

        batches = list(self.batches(run))
        self.assertEqual({b.branch_id for b in batches}, {self.ikeja.pk, self.lekki.pk})
        self.assertEqual([b.item_count for b in batches], [1, 1])
        self.assertEqual(sorted(b.total_amount for b in batches), [45_000, 72_000])
        for batch in batches:
            self.assertEqual(batch.purpose, PayoutPurpose.PAYROLL)
            self.assertFalse(WorkflowInstance.all_objects.for_document(batch).exists())
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)
        self.assertIsNone(run.disbursement_journal_id)
        self.assertEqual(self.line(run, self.ada).pay_state, State.SENT)

    def test_the_pay_request_hands_each_batch_to_a_worker_once_it_commits(self):
        run = self.posted_run()
        with patch.object(payroll_payouts, "enqueue_dispatch") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                self.pay(run)
        enqueue.assert_called_once()
        self.assertEqual(
            sorted(enqueue.call_args.args[0]), sorted(self.batches(run).values_list("pk", flat=True)))

    def test_each_branch_is_paid_from_its_own_money_with_the_fee_borne_by_it(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)

        ada_payout, bola_payout = self.payout_of(run, self.ada), self.payout_of(run, self.bola)
        self.assertEqual((ada_payout.status, bola_payout.status),
                         (PayoutStatus.PROCESSING, PayoutStatus.PROCESSING))
        self.assertEqual((ada_payout.beneficiary_account_number, ada_payout.beneficiary_bank_code),
                         ("0123456789", "058"))
        self.assertEqual(held.held_balance(self.ikeja.pk), 100_000 - 45_000 - FEE)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000 - 72_000 - FEE)
        self.assertEqual(
            HeldMovement.objects.get(payout=ada_payout, kind=HeldMovementKind.PAYOUT).amount,
            -(45_000 + FEE))
        self.assert_books_agree()

    def test_a_paid_line_books_net_wages_against_clearing_in_its_branch_with_the_fee(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.confirm(self.payout_of(run, self.ada))

        line = self.line(run, self.ada)
        self.assertEqual((line.pay_state, line.paid_via), (State.PAID, "PAYSTACK"))
        wages = JournalLine.objects.get(
            account__code="2330", debit=45_000, entry__entity=self.books, entry__status="POSTED")
        self.assertEqual(wages.entry.branch_id, self.ikeja.pk)
        clearing = JournalLine.objects.get(entry=wages.entry, account__code="1125")
        self.assertEqual(clearing.credit, 45_000)
        fee = JournalLine.objects.get(account__code="5500", entry__reference=line.payout_reference)
        self.assertEqual((fee.debit, fee.entry.branch_id), (FEE, self.ikeja.pk))
        self.assertTrue(Payslip.objects.filter(line=line).exists())
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)  # Lekki is not paid yet.
        self.assertEqual(
            PayrollRunBranch.objects.get(run=run, branch=self.ikeja).status, PayrollRunStatus.PAID)

    def test_the_run_reads_paid_when_every_line_is_and_the_held_check_balances(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.confirm(self.payout_of(run, self.ada))
        self.confirm(self.payout_of(run, self.bola))

        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        self.assertEqual(
            set(PayrollRunBranch.objects.filter(run=run).values_list("status", flat=True)),
            {PayrollRunStatus.PAID})
        self.assertEqual(Payslip.objects.filter(run=run).count(), 2)
        self.assert_books_agree()
        self.fake.balances["NGN"] = 100_000 + 178_000 - 45_000 - 72_000 - 2 * FEE
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (0, True))

    def test_a_failed_transfer_gives_the_money_back_and_the_line_can_be_retried(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        failed = self.payout_of(run, self.ada)
        self.confirm(failed, status=PayoutStatus.FAILED)

        line = self.line(run, self.ada)
        self.assertEqual(line.pay_state, State.FAILED)
        self.assertEqual(held.held_balance(self.ikeja.pk), 100_000)
        self.assertFalse(JournalLine.objects.filter(
            account__code="2330", debit=45_000, entry__entity=self.books).exists())
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)

        response = self.act(run, "payments/retry")
        self.assertEqual(response.status_code, 200, response.data)
        self.send(run)
        retried = self.payout_of(run, self.ada)
        self.assertNotEqual(retried.pk, failed.pk)
        self.assertEqual(retried.status, PayoutStatus.PROCESSING)
        self.confirm(retried)
        self.refresh(line)
        self.assertEqual(line.pay_state, State.PAID)
        self.assertEqual(held.held_balance(self.ikeja.pk), 100_000 - 45_000 - FEE)

    def test_retrying_sends_only_failed_lines_never_one_already_sent(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.assertEqual(self.act(run, "payments/retry").status_code, 422)
        self.assertEqual(self.act(run, "pay").status_code, 422)

    def test_a_tenant_paying_from_its_own_bank_is_unchanged(self):
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        for tag, branch in (("71", self.ikeja), ("72", self.lekki)):
            self.ops_bank(self.books, f"{branch.name} ops", branch, tag)
        run = self.posted_run()
        ikeja_bank = BankAccount.objects.get(entity=self.books, branch=self.ikeja, name="Ikeja Branch ops")
        lekki_bank = BankAccount.objects.get(entity=self.books, branch=self.lekki, name="Lekki Branch ops")
        response = self.act(run, "pay", {"bank_accounts": [ikeja_bank.pk, lekki_bank.pk]})
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        self.assertFalse(self.batches(run).exists())
        self.assertEqual(set(PayrollLine.objects.filter(run=run).values_list("pay_state", flat=True)), {""})
        self.assertTrue(JournalLine.objects.filter(
            account=ikeja_bank.gl_account, credit=45_000, entry__branch=self.ikeja).exists())

    def test_paying_from_the_bank_after_lines_went_through_the_provider_is_refused(self):
        run = self.posted_run()
        self.pay(run)
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        bank = self.ops_bank(self.books, "Ikeja ops", self.ikeja, "73")
        response = self.act(run, "pay", {"bank_accounts": [bank.pk]})
        self.assertEqual(response.status_code, 422, response.data)

    def test_a_run_with_staff_in_flight_cannot_be_voided(self):
        run = self.posted_run()
        self.pay(run)
        response = self.act(run, "cancel")
        self.assertEqual(response.status_code, 422, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.POSTED)

    def test_a_one_branch_tenant_pays_through_one_batch_and_reads_paid(self):
        self.fund(self.solo_parent, 100_000, books=self.solo_books)
        run = self.posted_run(client=TenantAPIClient(user=self.solo_bursar_user), books=self.solo_books)
        self.pay(run, client=TenantAPIClient(user=self.solo_bursar_user), books=self.solo_books)
        self.assertEqual(self.batches(run).count(), 1)
        self.send(run)
        self.confirm(self.payout_of(run, self.sade))
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)
        self.assertEqual(held.held_balance(self.solo_main.pk), 100_000 - 54_000 - FEE)
        self.assert_books_agree()


class WaitingForFundsTests(_PayrollFixture):
    """A branch that cannot cover its whole batch waits; the others still go."""

    def test_a_short_branch_waits_while_the_other_branch_is_paid(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 10_000)  # Lekki needs 72,000 + fee.
        run = self.posted_run()
        self.pay(run)
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            self.send(run)

        self.assertEqual(sent.call_count, 1)
        self.assertEqual(self.payout_of(run, self.ada).status, PayoutStatus.PROCESSING)
        bola_payout = self.payout_of(run, self.bola)
        self.assertEqual(bola_payout.status, PayoutStatus.PENDING)
        batch = self.batch_for(run, self.lekki)
        self.assertEqual(batch.status, PayoutBatchStatus.WAITING_FOR_FUNDS)
        line = self.line(run, self.bola)
        self.assertEqual(line.pay_state, State.WAITING_FOR_FUNDS)
        self.assertIn("holds", line.pay_note)
        self.assertEqual(held.held_balance(self.lekki.pk), 10_000)  # Nothing reserved.

    def test_the_sweep_sends_the_waiting_branch_once_funds_arrive_and_never_twice(self):
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 10_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.fund(self.adeyemi, 100_000)

        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            summary = payroll_payouts.sweep_payroll_batches()
            payroll_payouts.sweep_payroll_batches()
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(self.payout_of(run, self.bola).status, PayoutStatus.PROCESSING)
        self.assertEqual(self.line(run, self.bola).pay_state, State.SENT)
        batch = self.batch_for(run, self.lekki)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatchStatus.PROCESSING)
        self.assertEqual(held.held_balance(self.lekki.pk), 110_000 - 72_000 - FEE)

    def test_a_branch_never_pays_from_another_branchs_money(self):
        self.fund(self.okafor, 1_000_000)  # Ikeja is rich; Lekki holds nothing.
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.assertEqual(self.line(run, self.bola).pay_state, State.WAITING_FOR_FUNDS)
        self.assertEqual(held.held_balance(self.ikeja.pk), 1_000_000 - 45_000 - FEE)


class BankDetailsTests(_PayrollFixture):
    """Only a verified or confirmed account is sent to."""

    def test_an_unconfirmed_account_is_skipped_and_the_line_says_so(self):
        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).update(
            verification_state=StaffBankState.NEEDS_CONFIRMATION)
        self.fund(self.okafor, 100_000)
        self.fund(self.adeyemi, 178_000)
        run = self.posted_run()
        self.pay(run)

        self.assertEqual([b.branch_id for b in self.batches(run)], [self.ikeja.pk])
        line = self.line(run, self.bola)
        self.assertEqual(line.pay_state, State.NEEDS_BANK_DETAILS)
        self.assertIn("needs a second person", line.pay_note)
        self.assertIsNone(self.payout_of(run, self.bola))

    def test_a_staff_member_with_no_account_at_all_needs_bank_details(self):
        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).delete()
        run = self.posted_run()
        self.pay(run)
        self.assertEqual(self.line(run, self.bola).pay_state, State.NEEDS_BANK_DETAILS)

    def test_paying_again_once_a_second_person_confirms_sends_only_the_held_back_line(self):
        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).update(
            verification_state=StaffBankState.NEEDS_CONFIRMATION)
        run = self.posted_run()
        self.pay(run)
        confirm_staff_bank_account(
            account=StaffBankAccount.objects.get(pk=self.bola_bank.pk), actor_user=self.checker)
        self.pay(run)

        self.assertEqual(self.batches(run).count(), 2)
        self.assertEqual(self.line(run, self.bola).pay_state, State.SENT)
        self.assertEqual(PayoutInstruction.objects.filter(
            metadata__payroll_line_id=self.line(run, self.ada).pk).count(), 1)

    def test_an_account_changed_after_the_payment_was_prepared_is_not_paid(self):
        self.fund(self.okafor, 100_000)
        run = self.posted_run()
        self.pay(run)
        row = StaffBankAccount.objects.get(pk=self.ada_bank.pk)
        row.account_number = "0123456799"
        row.save()  # Resets to unchecked.
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            self.send(run)
        sent.assert_not_called()
        self.assertEqual(self.line(run, self.ada).pay_state, State.NEEDS_BANK_DETAILS)
        self.assertEqual(self.payout_of(run, self.ada).status, PayoutStatus.FAILED)
        self.assertEqual(held.held_balance(self.ikeja.pk), 100_000)

    def test_lines_the_provider_cannot_pay_are_recorded_as_paid_from_the_branchs_bank(self):
        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).delete()
        self.fund(self.okafor, 100_000)
        run = self.posted_run()
        self.pay(run)
        self.send(run)
        self.confirm(self.payout_of(run, self.ada))
        bank = self.ops_bank(self.books, "Lekki ops", self.lekki, "74")
        line = self.line(run, self.bola)

        response = self.act(run, "payments/record-paid-from-bank",
                            {"line_ids": [line.pk], "bank_account": bank.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.refresh(line)
        self.assertEqual((line.pay_state, line.paid_via), (State.PAID, "OWN_BANK"))
        self.assertTrue(JournalLine.objects.filter(
            account=bank.gl_account, credit=72_000, entry__branch=self.lekki).exists())
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)

    def test_a_line_in_flight_cannot_also_be_recorded_as_paid_from_the_bank(self):
        run = self.posted_run()
        self.pay(run)
        bank = self.ops_bank(self.books, "Ikeja ops", self.ikeja, "75")
        line = self.line(run, self.ada)
        response = self.act(run, "payments/record-paid-from-bank",
                            {"line_ids": [line.pk], "bank_account": bank.pk})
        self.assertEqual(response.status_code, 422, response.data)

    def test_the_bank_must_be_the_lines_own_branchs(self):
        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).delete()
        run = self.posted_run()
        self.pay(run)
        wrong = self.ops_bank(self.books, "Ikeja ops", self.ikeja, "76")
        response = self.act(run, "payments/record-paid-from-bank", {
            "line_ids": [self.line(run, self.bola).pk], "bank_account": wrong.pk})
        self.assertEqual(response.status_code, 422, response.data)


class ApprovalExemptionTests(_PayrollFixture):
    """Only a payroll batch of a posted run, and nothing a caller passes, skips the payout approval."""

    def test_a_vendor_batch_cannot_be_sent_without_its_approval(self):
        self.fund(self.adeyemi, 178_000)
        batch = self.vendor_payout(10_000, self.lekki_bank)
        payout = batch.instructions.get()
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            with self.assertRaises(PayoutApprovalRequiredError):
                services._dispatch_transfer(
                    payout, batch=batch, approved_instance=None, vendor=self.vendor)
            with self.assertRaises(PayoutApprovalRequiredError):
                services.submit_payout_batch(batch)
        sent.assert_not_called()
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_vendor_batch_dressed_as_payroll_is_still_refused(self):
        run = self.posted_run()
        batch = self.vendor_payout(10_000, self.lekki_bank)
        batch.metadata = {"payroll_run_id": run.pk}
        batch.save(update_fields=["metadata"])
        with self.assertRaises(PayoutApprovalRequiredError):
            services._dispatch_transfer(
                batch.instructions.get(), batch=batch, approved_instance=None, vendor=self.vendor)

    def test_a_payroll_batch_naming_no_posted_run_is_refused(self):
        run = self.posted_run()
        self.pay(run)
        batch = self.batches(run).first()
        batch.metadata = {}
        batch.save(update_fields=["metadata"])
        with self.assertRaises(PayoutApprovalRequiredError):
            payroll_payouts.dispatch_payroll_batch(batch.pk)
        PayrollRun.objects.filter(pk=run.pk).update(run_status=PayrollRunStatus.DRAFT)
        batch.metadata = {"payroll_run_id": run.pk}
        batch.save(update_fields=["metadata"])
        with self.assertRaises(PayoutApprovalRequiredError):
            payroll_payouts.dispatch_payroll_batch(batch.pk)

    def test_a_payroll_batch_is_not_sent_once_the_tenant_stops_paying_through_the_provider(self):
        run = self.posted_run()
        self.pay(run)
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        for batch in self.batches(run):
            with self.assertRaises(PayoutApprovalRequiredError):
                payroll_payouts.dispatch_payroll_batch(batch.pk)
        summary = payroll_payouts.sweep_payroll_batches()
        self.assertEqual(summary["abandoned"], 2)
        self.assertFalse(PayoutInstruction.objects.filter(
            batch__purpose=PayoutPurpose.PAYROLL, status=PayoutStatus.PENDING).exists())

    def test_a_payroll_batch_cannot_be_put_through_a_payout_approval(self):
        run = self.posted_run()
        self.pay(run)
        batch = self.batches(run).first()
        with self.assertRaises(PaymentStateError):
            services.submit_payout_batch_for_approval(batch, requested_by=self.bursar_user)
        with self.assertRaises(PaymentStateError):
            services.submit_payout_batch(batch, approved_instance=object())


class PayrollSecurityTests(_PayrollFixture):

    def test_a_caller_without_the_pay_key_is_refused_and_nothing_is_created(self):
        run = self.posted_run()
        poster = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "poster@corona.test"),
            "finance.payrollrun.view", "finance.payrollrun.post",
            tenant=self.tenant, role_key="poster-only"))
        for action in ("pay", "payments/retry", "payments/record-paid-from-bank"):
            self.assertEqual(self.act(run, action, client=poster).status_code, 403, action)
        self.assertFalse(self.batches(run).exists())

    def test_the_progress_needs_the_view_key(self):
        run = self.posted_run()
        nobody = TenantAPIClient(user=self.user_for(self.tenant, "nobody2@corona.test"))
        response = nobody.get(f"/v1/finance/payroll-runs/{run.pk}/payments/?entity={self.books.code}")
        self.assertEqual(response.status_code, 403)

    def test_another_tenants_run_cannot_be_paid_or_read(self):
        run = self.posted_run()
        rival = TenantAPIClient(user=self.grant(
            self.user_for(self.rival_tenant, "rival-pay@rival.test"), *PAY_KEYS,
            tenant=self.rival_tenant, role_key="rival-bursar"))
        self.assertEqual(
            self.act(run, "pay", client=rival, books=self.rival_books).status_code, 404)
        self.assertEqual(rival.get(
            f"/v1/finance/payroll-runs/{run.pk}/payments/?entity={self.rival_books.code}"
        ).status_code, 404)
        self.assertFalse(self.batches(run).exists())

    def test_a_branch_bound_caller_cannot_pay_a_run_that_covers_every_branch(self):
        run = self.posted_run()
        lekki = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "lekki-pay@corona.test"), *PAY_KEYS,
            tenant=self.tenant, role_key="lekki-bursar", branch=self.lekki))
        self.assertEqual(self.act(run, "pay", client=lekki).status_code, 403)
        self.assertFalse(self.batches(run).exists())

    def test_a_branch_bound_reader_sees_only_their_own_branchs_lines(self):
        run = self.posted_run()
        self.pay(run)
        lekki = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "lekki-read@corona.test"), "finance.payrollrun.view",
            tenant=self.tenant, role_key="lekki-reader", branch=self.lekki))
        response = lekki.get(f"/v1/finance/payroll-runs/{run.pk}/payments/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual([row["branch_id"] for row in data["branches"]], [self.lekki.pk])
        self.assertEqual([row["employee_id"] for row in data["lines"]], [self.bola.pk])
        self.assertEqual(data["counts"]["sent"], 1)
        self.assertEqual(data["branches"][0]["net_total"], 72_000)

    def test_progress_reports_each_state_and_hides_what_a_role_may_not_read(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        StaffBankAccount.objects.filter(pk=self.bola_bank.pk).update(
            verification_state=StaffBankState.FAILED)
        run = self.posted_run()
        self.pay(run)
        response = self.bursar.get(
            f"/v1/finance/payroll-runs/{run.pk}/payments/?entity={self.books.code}")
        counts = response.data["data"]["counts"]
        self.assertEqual((counts["sent"], counts["needs_bank_details"]), (1, 1))

        keys = install_declared_fields("finance.payrollrun")
        role = TenantRoleTemplate.objects.get(tenant=self.tenant, key="payroll-bursar")
        set_field_access(role, *keys, read=False, write=False)
        hidden = self.bursar.get(
            f"/v1/finance/payroll-runs/{run.pk}/payments/?entity={self.books.code}").data["data"]
        self.assertNotIn("counts", hidden)
        for row in hidden["branches"]:
            self.assertNotIn("net_total", row)
            self.assertNotIn("counts", row)
        for row in hidden["lines"]:
            self.assertNotIn("pay_state", row)
            self.assertNotIn("net_amount", row)
            self.assertNotIn("employee_name", row)


class PayrollRouteSettingTests(_PayrollFixture):
    """Choosing the route, and the custody rules that go with it."""

    def settings_url(self, books=None):
        return f"/v1/finance/settings/payroll/?entity={(books or self.books).code}"

    def patch_route(self, route, client=None, books=None):
        return (client or self.bursar).patch(
            self.settings_url(books), {"payment_route": route}, format="json")

    def test_the_default_route_is_the_branchs_own_bank(self):
        FinancePayrollSettings.objects.filter(entity=self.books).delete()
        data = self.bursar.get(self.settings_url()).data["data"]["settings"]
        self.assertEqual(data["payment_route"], "OWN_BANK")
        self.assertTrue(data["paystack_route_available"])
        self.assertEqual([o["value"] for o in data["payment_route_options"]], ["OWN_BANK", "PAYSTACK"])

    def test_a_held_tenant_chooses_the_provider_and_the_change_is_audited(self):
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        response = self.patch_route("PAYSTACK")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["settings"]["payment_route"], "PAYSTACK")
        history = response.data["data"]["history"]
        self.assertEqual(history[0]["changes"][0]["label"], "How staff are paid")

    def test_a_direct_tenant_cannot_choose_the_provider(self):
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        self.direct(self.tenant)
        response = self.patch_route("PAYSTACK")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("settle directly", str(response.data))
        self.assertEqual(
            FinancePayrollSettings.objects.get(entity=self.books).payment_route, "OWN_BANK")
        data = self.bursar.get(self.settings_url()).data["data"]["settings"]
        self.assertFalse(data["paystack_route_available"])

    def test_a_tenant_waiting_to_move_to_direct_cannot_choose_the_provider(self):
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        row = PaymentCustodySettings.objects.create(tenant=self.tenant, mode=CustodyMode.HELD)
        PaymentCustodySettings.objects.filter(pk=row.pk).update(
            pending_mode=CustodyMode.DIRECT, pending_from=custody.next_month_start(
                tenant_today(self.tenant)))
        self.assertEqual(self.patch_route("PAYSTACK").status_code, 400)

    def test_moving_to_direct_is_refused_while_staff_are_paid_through_the_provider(self):
        response = self.bursar.patch(
            f"/v1/payments/settings/custody/?entity={self.books.code}",
            {"mode": "DIRECT"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("payroll payment route", str(response.data))
        self.assertFalse(PaymentCustodySettings.objects.filter(
            tenant=self.tenant, pending_mode=CustodyMode.DIRECT).exists())

    def test_the_move_to_direct_is_allowed_once_staff_are_paid_from_the_bank_again(self):
        self.assertEqual(self.patch_route("OWN_BANK").status_code, 200)
        self.assertFalse(custody.payroll_uses_provider(self.tenant))
        with patch.object(custody, "branches_without_subaccount", return_value=[]):
            response = self.bursar.patch(
                f"/v1/payments/settings/custody/?entity={self.books.code}",
                {"mode": "DIRECT"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_pending_move_to_direct_waits_while_staff_are_still_paid_through_the_provider(self):
        row = PaymentCustodySettings.objects.create(tenant=self.tenant, mode=CustodyMode.HELD)
        today = tenant_today(self.tenant)
        PaymentCustodySettings.objects.filter(pk=row.pk).update(
            pending_mode=CustodyMode.DIRECT, pending_from=today)
        row.refresh_from_db()
        with patch.object(held, "branches_without_subaccount", create=True):
            with patch.object(custody, "branches_without_subaccount", return_value=[]):
                result = held.apply_custody_switch(row, today=today)
        self.assertEqual(result["result"], "waiting")
        self.assertIn("paid through the payment provider", str(result["reasons"]))

    def test_changing_the_route_needs_the_settings_update_key_and_the_whole_tenant(self):
        reader = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "set-read@corona.test"), "finance.settings.view",
            tenant=self.tenant, role_key="settings-reader"))
        self.assertEqual(self.patch_route("OWN_BANK", client=reader).status_code, 403)
        pinned = TenantAPIClient(user=self.grant(
            self.user_for(self.tenant, "set-lekki@corona.test"),
            "finance.settings.view", "finance.settings.update",
            tenant=self.tenant, role_key="settings-lekki", branch=self.lekki))
        self.assertEqual(self.patch_route("OWN_BANK", client=pinned).status_code, 403)
        self.assertEqual(
            FinancePayrollSettings.objects.get(entity=self.books).payment_route, "PAYSTACK")

    def test_an_unknown_route_is_refused(self):
        self.assertEqual(self.patch_route("CASH").status_code, 400)

    def test_another_tenants_settings_cannot_be_changed(self):
        rival = TenantAPIClient(user=self.grant(
            self.user_for(self.rival_tenant, "rival-set@rival.test"), *PAY_KEYS,
            tenant=self.rival_tenant, role_key="rival-set"))
        self.assertEqual(
            self.patch_route("PAYSTACK", client=rival, books=self.books).status_code, 404)
        self.assertEqual(
            FinancePayrollSettings.objects.get(entity=self.books).payment_route, "PAYSTACK")


class ApprovalGateTests(_PayrollFixture):
    """A run reaches the provider only if its approval route approved it; older runs go by bank."""

    def legacy_run(self, books=None, client=None):
        """A run posted before payroll approvals existed: posted, with no request at all."""
        from vs_finance.payroll_test_support import approve_for_posting

        books = books or self.books
        run = self.generated_run(client, books)
        approve_for_posting(run)
        posted = self.act(run, "post", client=client, books=books)
        self.assertEqual(posted.status_code, 200, posted.data)
        run.refresh_from_db()
        return run

    def test_paying_a_legacy_run_through_the_provider_is_refused_with_the_way_out(self):
        run = self.legacy_run()
        response = self.act(run, "pay")
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("Approve this run before paying it through Paystack", response.data["message"])
        self.assertIn("record it as paid from the branch's bank", response.data["message"])
        self.assertFalse(self.batches(run).exists())
        self.assertEqual(self.act(run, "payments/retry").status_code, 422)

    def test_a_legacy_runs_lines_can_be_recorded_as_paid_from_the_bank(self):
        run = self.legacy_run()
        bank = self.ops_bank(self.books, "Ikeja ops", self.ikeja, "77")
        line = self.line(run, self.ada)
        response = self.act(run, "payments/record-paid-from-bank",
                            {"line_ids": [line.pk], "bank_account": bank.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.refresh(line)
        self.assertEqual((line.pay_state, line.paid_via), (State.PAID, "OWN_BANK"))

    def test_a_legacy_run_is_still_paid_from_the_bank_by_a_tenant_paying_that_way(self):
        FinancePayrollSettings.objects.filter(entity=self.books).update(payment_route="OWN_BANK")
        run = self.legacy_run()
        banks = [self.ops_bank(self.books, "Ikeja ops", self.ikeja, "78"),
                 self.ops_bank(self.books, "Lekki ops", self.lekki, "79")]
        response = self.act(run, "pay", {"bank_accounts": [b.pk for b in banks]})
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_status, PayrollRunStatus.PAID)

    def test_an_approved_run_is_paid_through_the_provider_and_an_unapproved_batch_is_not_sent(self):
        run = self.posted_run()
        self.pay(run)
        with patch("vs_finance.approvals.payroll_run_approved", return_value=False):
            for batch in self.batches(run):
                with self.assertRaises(PayoutApprovalRequiredError):
                    payroll_payouts.dispatch_payroll_batch(batch.pk)
        self.assertEqual(self.line(run, self.ada).pay_state, State.SENT)
