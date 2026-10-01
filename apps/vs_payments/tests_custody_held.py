"""The platform holds a held-mode tenant's online money for each branch, and pays it on.

Corona runs Ikeja, Lekki and Yaba and holds its online money with the platform;
Single Site runs one branch. Mrs Adeyemi pays Lekki N1,800 online and the provider
keeps N20: Lekki's books show N1,800 in gateway clearing and the platform's books
(CodeX, Lagos branch) show N1,780 held for Corona Lekki Branch. Lekki can pay a
supplier online up to that N1,780 and not a kobo of Ikeja's. The settlement run
prepares CodeX's payout of what is left to Lekki's Zenith account, once, less
Paystack's transfer fee, which Lekki bears; a CodeX operator puts it forward and
two CodeX people approve it, and when the transfer is confirmed Lekki's books
record the money arriving and both sets of books read zero for Lekki. Nobody at
Corona submits or approves anything. A chargeback on held money comes off
Lekki's held balance at once, and what Lekki did not hold it owes CodeX from its
next settlement. A tenant moving to direct custody waits until nothing is held,
then gets new virtual account numbers; a parent still paying into an old one is
credited and the money passed on. A direct tenant's online payments are not
refunded online, and its chargebacks are recorded and raised, never booked.
"""
from __future__ import annotations

import datetime
import itertools
from unittest.mock import patch

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.models import Account, JournalEntry, JournalLine
from vs_finance.seed import seed_chart_of_accounts, seed_fiscal_year
from vs_procurement.models import Vendor
from vs_tenants.models import Branch

from . import custody, held, held_reconciliation, services, settlement, webhooks
from .constants import (
    CollectionStatus,
    CustodyMode,
    HeldMovementKind,
    HeldSettlementStatus,
    PaymentAuditAction,
    PayoutPurpose,
    PayoutStatus,
    VirtualAccountStatus,
    WebhookStatus,
)
from .custody import OnlineRefundsNotOfferedError
from .exceptions import PaymentStateError, PayoutApprovalRequiredError
from .models import (
    CollectionIntent,
    HeldBalance,
    PayoutBatch,
    HeldMovement,
    HeldReconciliation,
    HeldSettlement,
    PaymentCustodySettings,
    PaymentEvent,
    VirtualAccount,
)
from .tests_custody import _CustodyFixture

_people = itertools.count(1)


def _approval_passes(batch, instance):
    """Stand-in for the approval check, which the payout approval tests cover."""
    return instance


class _HeldFixture(_CustodyFixture):
    """Corona and Single Site held by the platform, with the platform's own books open."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.platform = held.platform_books()
        seed_chart_of_accounts(cls.platform)
        seed_fiscal_year(cls.platform)
        cls.lagos = Branch.all_objects.get(tenant=cls.platform.tenant)
        for bank, code in ((cls.ikeja_bank, "058"), (cls.lekki_bank, "057"), (cls.solo_bank, "044")):
            bank.settlement_bank_code = code
            bank.save(update_fields=["settlement_bank_code"])
        cls.vendor = Vendor.objects.create(
            entity=cls.books, code="SUPPH", name="Adeola Printers",
            payable_account=Account.objects.get(entity=cls.books, code="2100"),
            default_expense_account=Account.objects.get(entity=cls.books, code="5300"),
            bank_name="GTBank", bank_code="058", bank_account_name="Adeola Printers",
            bank_account_number="0123456789", kyc_status="VERIFIED",
        )

    # -- helpers --------------------------------------------------------------- #

    def yesterday(self, *intents):
        """Move confirmations into yesterday, before the regular run's cutoff."""
        CollectionIntent.objects.filter(pk__in=[i.pk for i in intents]).update(
            confirmed_at=timezone.now() - datetime.timedelta(days=1))

    def dispatch(self, batch):
        with patch.object(services, "_validate_approved_instance", side_effect=_approval_passes):
            services.submit_payout_batch(batch, approved_instance=object())
        return batch.instructions.get()

    def confirm(self, payout, status=PayoutStatus.PAID, amount=None):
        self.fake.forced_status[payout.reference] = status
        if amount is not None:
            self.fake.forced_amount[payout.reference] = amount
        return services.confirm_payout(payout)

    def vendor_payout(self, amount, bank):
        batch = services.create_payout_batch(
            entity=self.books, items=[{"amount": amount, "vendor": self.vendor}],
            source_account=bank.gl_account)
        return batch

    def platform_line_total(self, code):
        account = Account.objects.get(entity=self.platform, code=code)
        lines = JournalLine.objects.filter(account=account, entry__status="POSTED")
        return sum(line.debit - line.credit for line in lines)

    def assert_books_agree(self):
        """The platform's books equal the sub-ledger, kobo for kobo.

        What branches hold is the client-funds liability, what they owe is the
        owed-by-clients asset, and the provider balance is the two netted.
        """
        balances = list(HeldBalance.objects.values_list("balance", flat=True))
        self.assertEqual(held.platform_liability_balance(), sum(b for b in balances if b > 0))
        self.assertEqual(held.platform_owed_balance(), -sum(b for b in balances if b < 0))
        self.assertEqual(self.platform_line_total("1127"), sum(balances))


class HeldCollectionTests(_HeldFixture):
    """A held payment raises what the platform holds for its branch, in both books."""

    def test_a_held_payment_raises_the_branchs_held_balance_in_both_books(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)

        self.assertTrue(intent.held_by_platform)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assertEqual(held.held_balance(self.ikeja.pk), 0)
        movement = HeldMovement.objects.get(collection=intent)
        self.assertEqual((movement.kind, movement.amount, movement.tenant_id, movement.branch_id),
                         (HeldMovementKind.COLLECTION, 178_000, self.tenant.pk, self.lekki.pk))
        journal = movement.platform_journal
        self.assertEqual((journal.entity, journal.branch, journal.status),
                         (self.platform, self.lagos, "POSTED"))
        self.assertIn("Lekki Branch", journal.narration)
        self.assertEqual(self.platform_line_total("2180"), -178_000)
        self.assertEqual(self.platform_line_total("1127"), 178_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 180_000)
        self.assert_books_agree()

    def test_a_one_branch_tenants_payment_is_held_for_its_branch(self):
        self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.assertEqual(held.held_balance(self.solo_main.pk), 39_400)

    def test_a_payment_settling_to_the_branchs_bank_is_not_held(self):
        self.settle_directly()
        intent = self.paid(self.books, self.adeyemi, 180_000)
        self.assertFalse(intent.held_by_platform)
        self.assertFalse(HeldMovement.objects.exists())

    def test_a_held_payment_is_not_settled_from_a_statement_line(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        with self.assertRaises(PaymentStateError) as caught:
            settlement.settle_collections(self.line(self.lekki_bank, 178_000), [intent.pk])
        self.assertIn("held by the platform", str(caught.exception))

    def test_a_movement_the_platforms_books_cannot_take_waits_and_posts_later(self):
        with patch.object(held, "platform_books", return_value=None):
            intent = self.paid(self.books, self.adeyemi, 180_000)
        movement = HeldMovement.objects.get(collection=intent)
        self.assertIsNone(movement.platform_journal)
        self.assertIn("not set up", movement.journal_error)
        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)  # The payment still booked.

        self.assertEqual(held.post_pending_platform_journals(), {"posted": 1, "waiting": 0})
        movement.refresh_from_db()
        self.assertEqual(movement.platform_journal.entity, self.platform)
        self.assert_books_agree()

    def test_held_accounts_are_kept_by_the_ledger_and_only_in_the_platforms_books(self):
        from vs_finance.account_mappings import account_mapping_snapshot
        from vs_finance.control_accounts import control_accounts

        self.paid(self.books, self.adeyemi, 10_000)
        kept = control_accounts(self.platform)
        for code in ("2180", "1127"):
            self.assertIn(Account.objects.get(entity=self.platform, code=code).pk, kept)
        keys = {row["key"] for row in account_mapping_snapshot(self.books)}
        self.assertNotIn("CLIENT_FUNDS_HELD", keys)
        self.assertIn("CLIENT_FUNDS_HELD",
                      {row["key"] for row in account_mapping_snapshot(self.platform)})


class SettlementRunTests(_HeldFixture):
    """The run pays each branch what is held for it, once, and books both sides."""

    def settle(self, held_settlement, amount=None):
        payout = self.dispatch(held_settlement.batch)
        return self.confirm(payout, amount=amount if amount is not None else held_settlement.amount)

    def test_the_run_pays_each_branch_once_and_books_both_sides(self):
        lekki_a = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        lekki_b = self.paid(self.books, self.adeyemi, 50_000, fee=750)
        ikeja = self.paid(self.books, self.okafor, 100_000, fee=1_500)
        solo = self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.yesterday(lekki_a, lekki_b, ikeja, solo)

        summary = held.run_settlements()
        self.assertEqual(len(summary["built"]), 3)
        lekki_run = HeldSettlement.objects.get(branch=self.lekki)
        self.assertEqual((lekki_run.gross, lekki_run.fees, lekki_run.amount),
                         (230_000, 2_750, 227_250))
        batch = lekki_run.batch  # CodeX's document, in CodeX's books.
        self.assertEqual(
            (batch.purpose, batch.entity, batch.branch, batch.total_amount, batch.status),
            (PayoutPurpose.SETTLEMENT, self.platform, self.lagos, 227_250, "DRAFT"))
        self.assertEqual(batch.source_account.code, "1127")
        self.assertFalse(PayoutBatch.objects.filter(entity=self.books).exists())
        line = batch.instructions.get()
        self.assertEqual((line.beneficiary_account_number, line.beneficiary_bank_code),
                         (self.lekki_bank.account_number, "057"))
        self.assertEqual(HeldSettlement.objects.get(branch=self.ikeja).amount, 98_500)
        self.assertEqual(HeldSettlement.objects.get(branch=self.solo_main).amount, 39_400)

        self.assertEqual(held.run_settlements()["built"], [])  # Idempotent: nothing twice.

        payout = self.settle(lekki_run)
        self.assertEqual(payout.status, PayoutStatus.PAID)
        self.assertIsNone(payout.vendor_payment_id)
        lekki_run.refresh_from_db()
        self.assertEqual(lekki_run.status, HeldSettlementStatus.PAID)
        entry = lekki_run.settlement_journal
        self.assertEqual((entry.entity, entry.branch_id), (self.books, self.lekki.pk))
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 227_250)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        charges = Account.objects.get(entity=self.books, code="5500")
        self.assertEqual(self.balance(charges, self.lekki), 2_750)
        for intent in (lekki_a, lekki_b):
            intent.refresh_from_db()
            self.assertFalse(intent.awaits_settlement)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)
        self.assertEqual(held.held_balance(self.ikeja.pk), 98_500)
        self.assert_books_agree()

        tomorrow = tenant_today(self.tenant) + datetime.timedelta(days=1)
        again, note = held.build_settlement(self.books, self.lekki.pk, today=tomorrow)
        self.assertIsNone(again)
        self.assertIn("nothing waiting", note)

    def test_a_one_branch_tenant_is_paid_into_its_only_account(self):
        intent = self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.solo_books, self.solo_main.pk)
        self.settle(run)
        run.refresh_from_db()
        self.assertEqual(run.settlement_journal.branch_id, self.solo_main.pk)
        self.assertEqual(self.balance(self.solo_bank.gl_account), 39_400)
        self.assertEqual(self.balance(self.clearing(self.solo_books)), 0)

    def test_a_payment_confirmed_today_waits_for_the_next_run(self):
        self.paid(self.books, self.adeyemi, 180_000)
        run, note = held.build_settlement(self.books, self.lekki.pk)
        self.assertIsNone(run)
        self.assertIn("nothing waiting", note)

    def test_the_interval_spaces_the_runs(self):
        custody.update_custody_settings(entity=self.books, data={"settlement_interval_days": 3})
        first = self.paid(self.books, self.adeyemi, 180_000)
        self.yesterday(first)
        today = tenant_today(self.tenant)
        run, _ = held.build_settlement(self.books, self.lekki.pk, today=today)
        self.settle(run)
        self.paid(self.books, self.adeyemi, 50_000)

        early, note = held.build_settlement(
            self.books, self.lekki.pk, today=today + datetime.timedelta(days=2))
        self.assertIsNone(early)
        self.assertIn("due 3 day(s)", note)
        due, _ = held.build_settlement(
            self.books, self.lekki.pk, today=today + datetime.timedelta(days=3))
        self.assertEqual(due.gross, 50_000)

    def test_a_failed_transfer_releases_its_payments_and_balance(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        payout = self.dispatch(run.batch)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)

        self.confirm(payout, status=PayoutStatus.FAILED)
        run.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(run.status, HeldSettlementStatus.FAILED)
        self.assertIsNone(intent.held_settlement)
        self.assertTrue(intent.awaits_settlement)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assert_books_agree()

        retry, _ = held.build_settlement(
            self.books, self.lekki.pk, today=tenant_today(self.tenant) + datetime.timedelta(days=1))
        self.assertEqual(retry.amount, 178_000)

    def test_payouts_spent_part_so_the_settlement_sends_the_rest(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(intent)
        payout = self.dispatch(self.vendor_payout(50_000, self.lekki_bank))
        self.confirm(payout, amount=50_000)
        # The supplier was paid from the provider balance: clearing, not Zenith.
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 130_000)
        self.assertEqual(held.held_balance(self.lekki.pk), 128_000)

        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.assertEqual(run.amount, 128_000)
        self.settle(run)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 128_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)
        self.assert_books_agree()

    def test_a_settlement_goes_through_the_payout_approval(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        with self.assertRaises(PayoutApprovalRequiredError):
            services.submit_payout_batch(run.batch)
        self.assertEqual(run.batch.instructions.get().status, PayoutStatus.PENDING)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_changed_collection_account_stops_the_transfer(self):
        intent = self.paid(self.books, self.adeyemi, 180_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.lekki_bank.account_number = "9999999999"
        self.lekki_bank.save(update_fields=["account_number"])
        with self.assertRaises(PaymentStateError) as caught:
            self.dispatch(run.batch)
        self.assertIn("collection account changed", str(caught.exception))
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_branch_without_a_ready_account_is_told_why(self):
        yaba_parent = self.customer(self.books, "CYABA", self.yaba)
        yaba_bank = self.bank(self.books, "1133", "Yaba UBA", self.yaba)
        intent = self.paid(self.books, yaba_parent, 20_000)
        self.yesterday(intent)
        run, note = held.build_settlement(self.books, self.yaba.pk)
        self.assertIsNone(run)
        self.assertIn("Yaba UBA is not set up with the payment provider", note)
        yaba_bank.settlement_bank_code = "033"
        yaba_bank.save(update_fields=["settlement_bank_code"])
        run, _ = held.build_settlement(self.books, self.yaba.pk)
        self.assertEqual(run.gross, 20_000)


    def test_the_branch_bears_the_transfer_fee(self):
        self.fake.transfer_fee_kobo = 5_000
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.assertEqual((run.transfer_fee, run.amount), (5_000, 173_000))
        self.settle(run)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 173_000)
        charges = Account.objects.get(entity=self.books, code="5500")
        self.assertEqual(self.balance(charges, self.lekki), 7_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        # CodeX's provider balance fell by what was sent and the fee it paid.
        self.assertEqual(held.held_balance(self.lekki.pk), 0)
        self.assertEqual(self.platform_line_total("1127"), 0)
        self.assert_books_agree()


class PlatformSettlementApprovalTests(_HeldFixture):
    """A CodeX operator puts a settlement forward and two CodeX people approve it."""

    @classmethod
    def setUpTestData(cls):
        from vs_workflow.constants import GroupMemberKind
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        from .approvals import ensure_settlement_approval_template
        from .constants import WF_SETTLEMENT_APPROVER_GROUP

        super().setUpTestData()
        codex = cls.platform.tenant
        cls.operator = cls.grant(
            cls.user_for(codex, "ops@codex.test"),
            "payments.platform_settlement.view", "payments.platform_settlement.submit",
            tenant=codex, role_key="held-ops")
        ensure_settlement_approval_template(codex)
        group = WorkflowApproverGroup.all_objects.get(tenant=codex, code=WF_SETTLEMENT_APPROVER_GROUP)
        cls.approvers = []
        for email in ("chioma@codex.test", "bola@codex.test"):
            user = cls.user_for(codex, email)
            WorkflowApproverGroupMember.objects.create(
                group=group, kind=GroupMemberKind.USER, user=user)
            cls.approvers.append(user)
        cls.bursar = cls.grant(
            cls.user_for(cls.tenant, "bursar@corona.test"),
            "payments.platform_settlement.view", "payments.platform_settlement.submit",
            "payments.payout_batch.submit", tenant=cls.tenant, role_key="corona-bursar")

    def prepared(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        return run

    def test_codex_puts_it_forward_and_two_codex_people_release_it(self):
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.models import WorkflowInstance
        from vs_workflow.services import actions as wf_actions

        run = self.prepared()
        response = TenantAPIClient(user=self.operator).post(
            f"/v1/payments/platform/held-settlements/{run.pk}/submit/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        instance = WorkflowInstance.all_objects.for_document(run.batch).get()
        self.assertEqual(instance.tenant_id, self.platform.tenant_id)
        self.assertEqual(instance.template.code, "held-settlement")

        first, second = self.approvers
        wf_actions.record_action(instance.id, first, ActionEnum.APPROVED)
        payout = run.batch.instructions.get()
        self.assertEqual(payout.status, PayoutStatus.PENDING)  # One person is not enough.
        with self.captureOnCommitCallbacks(execute=True):
            wf_actions.record_action(instance.id, second, ActionEnum.APPROVED)
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)

        self.confirm(payout, amount=178_000)
        run.refresh_from_db()
        self.assertEqual(run.status, HeldSettlementStatus.PAID)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 178_000)
        self.assert_books_agree()

    def test_one_approval_does_not_release_even_a_small_settlement(self):
        """A N1,780 settlement still needs two CodeX people, though a payout that size needs one."""
        from vs_workflow.constants import WorkflowInstanceStatus
        from vs_workflow.constants import WorkflowStageAction as ActionEnum
        from vs_workflow.models import WorkflowInstance, WorkflowStageAction

        run = self.prepared()
        instance = held.submit_settlement(run, requested_by=self.operator)
        stage_instance = instance.stage_instances.order_by("pk").first()
        WorkflowStageAction.objects.create(
            stage_instance=stage_instance, actor=self.approvers[0], action=ActionEnum.APPROVED,
            attempt=stage_instance.attempt)
        WorkflowInstance.all_objects.filter(pk=instance.pk).update(
            status=WorkflowInstanceStatus.APPROVED)
        instance.refresh_from_db()
        with self.assertRaises(PayoutApprovalRequiredError) as caught:
            services._validate_approved_instance(run.batch, instance)
        self.assertIn("two distinct human approvers", str(caught.exception))

    def test_the_school_neither_submits_nor_lists_platform_settlements(self):
        run = self.prepared()
        bursar = TenantAPIClient(user=self.bursar)
        self.assertEqual(bursar.get("/v1/payments/platform/held-settlements/").status_code, 403)
        self.assertEqual(bursar.post(
            f"/v1/payments/platform/held-settlements/{run.pk}/submit/", {},
            format="json").status_code, 403)
        # Nor through the tenant's own payout route: the batch is not in its books.
        response = bursar.post(
            f"/v1/payments/payout-batches/{run.batch_id}/submit-for-approval/"
            f"?entity={self.books.code}", {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_codex_lists_every_tenants_settlements_due(self):
        self.prepared()
        solo = self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.yesterday(solo)
        held.build_settlement(self.solo_books, self.solo_main.pk)
        response = TenantAPIClient(user=self.operator).get("/v1/payments/platform/held-settlements/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = {(row["tenant"], row["branch_name"]) for row in response.data["data"]}
        self.assertEqual(rows, {(self.tenant.slug, "Lekki Branch"),
                                (self.solo_tenant.slug, "Main Branch")})
        one = TenantAPIClient(user=self.operator).get(
            f"/v1/payments/platform/held-settlements/?client={self.solo_tenant.slug}")
        self.assertEqual([row["tenant"] for row in one.data["data"]], [self.solo_tenant.slug])
        unkeyed = TenantAPIClient(user=self.user_for(self.platform.tenant, "nobody@codex.test"))
        self.assertEqual(unkeyed.get("/v1/payments/platform/held-settlements/").status_code, 403)


class HeldChargebackTests(_HeldFixture):
    """A chargeback on held money comes off the branch at once, in both books."""

    def chargeback(self, intent, amount, event="charge.dispute.create", resolution=""):
        body, headers = self.fake.build_webhook(
            event=event, reference=intent.reference,
            status="resolved" if resolution else "awaiting-merchant-feedback", amount=amount,
            resolution=resolution)
        with patch("vs_payments.alerts._notify", return_value=["n"]) as notify:
            event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
            webhooks.process_stored_event(event.pk)
        return notify

    def resolve(self, intent, resolution):
        """Paystack's resolution: ``declined`` is a dispute won, ``merchant-accepted`` one lost."""
        return self.chargeback(intent, 0, event="charge.dispute.resolve", resolution=resolution)

    def resolution_audit(self):
        return PaymentEvent.objects.get(action=PaymentAuditAction.PROVIDER_DISPUTE_RESOLVED)

    def test_a_dispute_won_gives_the_chargeback_back_in_both_books_once(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.chargeback(intent, 30_000)
        notify = self.resolve(intent, "declined")

        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        won = HeldMovement.objects.get(collection=intent, kind=HeldMovementKind.DISPUTE_WON)
        self.assertEqual((won.amount, won.tenant_journal.branch_id), (30_000, self.lekki.pk))
        chargebacks = Account.objects.get(entity=self.books, code="5520")
        self.assertEqual(self.balance(chargebacks, self.lekki), 0)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 180_000)
        self.assertEqual(self.platform_line_total("2180"), -178_000)
        self.assertEqual(self.resolution_audit().metadata["outcome"], "WON")
        context = notify.call_args_list[0].kwargs["context"]
        self.assertEqual(context["kind_label"], "Chargeback won")
        self.assertIn("₦300.00 was given back", context["booking"])

        self.assertEqual(held.restore_held_chargeback(intent), won)  # Once per dispute.
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assert_books_agree()

    def test_a_won_dispute_repays_the_shortfall_first(self):
        first = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(first)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.confirm(self.dispatch(run.batch), amount=178_000)
        self.paid(self.books, self.adeyemi, 50_000, fee=0)
        self.chargeback(first, 180_000)
        self.assertEqual(held.held_balance(self.lekki.pk), -130_000)
        self.assertEqual(held.platform_owed_balance(), 130_000)

        notify = self.resolve(first, "declined")
        self.assertEqual(held.held_balance(self.lekki.pk), 50_000)
        self.assertEqual(held.platform_owed_balance(), 0)
        self.assertEqual(held.platform_liability_balance(), 50_000)
        won = HeldMovement.objects.get(collection=first, kind=HeldMovementKind.DISPUTE_WON)
        self.assertEqual(held.repaid_by_restore(won), 130_000)
        self.assertIn("₦1,300.00 of it repaid what the branch owed",
                      notify.call_args_list[0].kwargs["context"]["booking"])
        self.assert_books_agree()

    def test_a_dispute_lost_stays_as_booked(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.chargeback(intent, 30_000)
        notify = self.resolve(intent, "merchant-accepted")
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000)
        self.assertFalse(HeldMovement.objects.filter(kind=HeldMovementKind.DISPUTE_WON).exists())
        chargebacks = Account.objects.get(entity=self.books, code="5520")
        self.assertEqual(self.balance(chargebacks, self.lekki), 30_000)
        self.assertEqual(self.resolution_audit().metadata["outcome"], "LOST")
        self.assertEqual(notify.call_args_list[0].kwargs["context"]["kind_label"], "Chargeback lost")
        self.assert_books_agree()

    def test_a_direct_tenants_resolution_is_recorded_only(self):
        self.settle_directly()
        intent = self.paid(self.books, self.adeyemi, 180_000)
        self.chargeback(intent, 30_000)
        self.resolve(intent, "declined")
        self.assertFalse(HeldMovement.objects.exists())
        audit_row = self.resolution_audit()
        self.assertEqual((audit_row.metadata["outcome"], audit_row.metadata["custody_mode"]),
                         ("WON", CustodyMode.DIRECT))

    def test_a_chargeback_within_the_held_balance(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        notify = self.chargeback(intent, 30_000)
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000)
        movement = HeldMovement.objects.get(collection=intent, kind=HeldMovementKind.DISPUTE)
        self.assertEqual((movement.amount, movement.tenant_journal.branch_id),
                         (-30_000, self.lekki.pk))
        chargebacks = Account.objects.get(entity=self.books, code="5520")
        self.assertEqual(self.balance(chargebacks, self.lekki), 30_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 150_000)
        self.assertIn("taken from the branch's held balance",
                      notify.call_args_list[0].kwargs["context"]["booking"])
        self.chargeback(intent, 30_000, event="charge.dispute.remind")  # Books nothing again.
        self.assertEqual(held.held_balance(self.lekki.pk), 148_000)
        self.assert_books_agree()

    def test_a_shortfall_is_owed_and_taken_from_the_next_settlement(self):
        first = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(first)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        self.confirm(self.dispatch(run.batch), amount=178_000)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)

        notify = self.chargeback(first, 180_000)
        self.assertEqual(held.held_balance(self.lekki.pk), -180_000)
        self.assertEqual(held.platform_owed_balance(), 180_000)
        self.assertIn("₦1,800.00 of it is owed to the platform",
                      notify.call_args_list[0].kwargs["context"]["booking"])
        self.assert_books_agree()
        refused = self.dispatch(self.vendor_payout(1_000, self.lekki_bank))
        self.assertEqual(refused.status, PayoutStatus.FAILED)

        second = self.paid(self.books, self.adeyemi, 200_000, fee=0)
        self.yesterday(second)
        self.assertEqual(held.held_balance(self.lekki.pk), 20_000)
        self.assertEqual(held.platform_owed_balance(), 0)
        later, _ = held.build_settlement(
            self.books, self.lekki.pk, today=tenant_today(self.tenant) + datetime.timedelta(days=1))
        self.assertEqual(later.amount, 20_000)
        self.confirm(self.dispatch(later.batch), amount=20_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 0)
        self.assertEqual(held.held_balance(self.lekki.pk), 0)
        self.assert_books_agree()


class HeldFundsCheckTests(_HeldFixture):
    """A held branch's online payout is refused above what is held for it."""

    def test_a_payout_above_the_held_balance_is_refused_and_sends_nothing(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        batch = self.vendor_payout(200_000, self.lekki_bank)
        with patch.object(self.fake, "create_transfer", wraps=self.fake.create_transfer) as sent:
            payout = self.dispatch(batch)
        sent.assert_not_called()
        self.assertEqual(payout.status, PayoutStatus.FAILED)
        self.assertIn("Lekki Branch has ₦1,780.00 of online payments held", payout.failure_reason)
        self.assertIn("needs ₦2,000.00", payout.failure_reason)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assertTrue(PaymentEvent.objects.filter(
            action=PaymentAuditAction.HELD_FUNDS_REFUSED, reference=payout.reference,
            succeeded=False).exists())

    def test_a_payout_within_the_balance_goes_and_a_failure_gives_it_back(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        payout = self.dispatch(self.vendor_payout(50_000, self.lekki_bank))
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(held.held_balance(self.lekki.pk), 128_000)
        self.assert_books_agree()

        self.confirm(payout, status=PayoutStatus.FAILED)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assertEqual(HeldMovement.objects.filter(
            payout=payout, kind=HeldMovementKind.RELEASE).get().amount, 50_000)
        self.assert_books_agree()

    def test_one_branchs_money_never_pays_anothers_supplier(self):
        self.paid(self.books, self.adeyemi, 180_000)
        payout = self.dispatch(self.vendor_payout(10_000, self.ikeja_bank))
        self.assertEqual(payout.status, PayoutStatus.FAILED)
        self.assertIn("Ikeja Branch has ₦0.00", payout.failure_reason)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_an_opening_balance_funds_a_branch_once_and_is_audited(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        operator = self.user_for(self.platform.tenant, "ada@codex.test")
        call_command(
            "record_held_opening_balance", tenant=self.tenant.slug, branch=self.ikeja.pk,
            amount=30_000, by="ada@codex.test", reason="Paystack balance at go-live",
            stdout=StringIO())
        event = PaymentEvent.objects.get(action=PaymentAuditAction.HELD_OPENING_BALANCE)
        self.assertEqual((event.actor_user, event.metadata["operator"], event.metadata["reason"]),
                         (operator, "ada@codex.test", "Paystack balance at go-live"))
        with self.assertRaises(CommandError) as caught:
            call_command(
                "record_held_opening_balance", tenant=self.tenant.slug, branch=self.ikeja.pk,
                amount=5_000, by="ada@codex.test", reason="again", stdout=StringIO())
        self.assertIn("already has its opening held balance", str(caught.exception))
        with self.assertRaises(CommandError):  # A school user is not a platform operator.
            self.user_for(self.tenant, "bursar@corona.test")
            call_command(
                "record_held_opening_balance", tenant=self.tenant.slug, branch=self.lekki.pk,
                amount=5_000, by="bursar@corona.test", reason="x", stdout=StringIO())

        payout = self.dispatch(self.vendor_payout(10_000, self.ikeja_bank))
        self.assertEqual(payout.status, PayoutStatus.PROCESSING)
        self.assertEqual(held.held_balance(self.ikeja.pk), 20_000)
        self.assert_books_agree()


class DirectRefundAndDisputeTests(_HeldFixture):
    """A direct tenant refunds from the bank; a chargeback is raised, never booked."""

    def test_an_online_refund_is_refused_for_a_direct_tenant_only(self):
        from vs_finance.credit_notes import check_refund_method

        check_refund_method(self.books, "ONLINE")  # Held: allowed.
        self.direct(self.tenant)
        with self.assertRaises(OnlineRefundsNotOfferedError) as caught:
            check_refund_method(self.books, "ONLINE")
        self.assertEqual(caught.exception.http_status, 409)
        self.assertIn("Refund the payer from the branch's bank and record the refund",
                      str(caught.exception))
        check_refund_method(self.books, "BANK_TRANSFER")  # Recording a bank refund stays open.

    def test_a_chargeback_is_recorded_and_raised_not_booked(self):
        self.settle_directly()
        intent = self.paid(self.books, self.adeyemi, 180_000)
        journals = JournalEntry.objects.count()
        body, headers = self.fake.build_webhook(
            event="charge.dispute.create", reference=intent.reference, status="awaiting-merchant-feedback",
            amount=180_000)
        with patch("vs_payments.alerts._notify", return_value=["n"]) as notify:
            event = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
            webhooks.process_stored_event(event.pk)
        event.refresh_from_db()
        self.assertEqual((event.status, event.collection_id), (WebhookStatus.PROCESSED, intent.pk))
        self.assertEqual(JournalEntry.objects.count(), journals)
        audit_row = PaymentEvent.objects.get(action=PaymentAuditAction.PROVIDER_DISPUTE_RECEIVED)
        self.assertEqual(audit_row.metadata["custody_mode"], CustodyMode.DIRECT)
        tenants = [call.kwargs["tenant"] for call in notify.call_args_list]
        self.assertEqual({t.kind for t in tenants}, {"PLATFORM", self.tenant.kind})
        self.assertIn("refund the payer from that bank",
                      notify.call_args_list[0].kwargs["context"]["guidance"])

    def test_the_chargeback_alarm_has_a_template_on_every_channel(self):
        """A registered event with no template is delivered to nobody, silently."""
        from django.core.management import call_command

        from vs_notifications.models import NotificationEventType, NotificationTemplate

        call_command("seed_notification_templates", verbosity=0)
        event_type = NotificationEventType.objects.get(key="payments.dispute_received")
        for channel in event_type.supported_channels:
            self.assertTrue(NotificationTemplate.objects.filter(
                event_type=event_type, channel=channel).exists(), channel)


class CustodySwitchTests(_HeldFixture):
    """Held to direct waits for nothing held, then reissues virtual accounts."""

    def pending(self, tenant, mode):
        row, _ = PaymentCustodySettings.objects.update_or_create(
            tenant=tenant, defaults={"pending_mode": mode, "pending_from": tenant_today(tenant)})
        return row

    def subaccount(self, bank, code):
        bank.gateway_subaccount_code, bank.gateway_subaccount_provider = code, "PAYSTACK"
        bank.save(update_fields=["gateway_subaccount_code", "gateway_subaccount_provider"])

    def test_held_to_direct_waits_for_zero_then_reissues_virtual_accounts(self):
        self.subaccount(self.solo_bank, "ACCT_SOLO")
        old = services.create_virtual_account(entity=self.solo_books, customer=self.solo_parent)
        self.assertEqual(old.settlement_subaccount, "")
        self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        row = self.pending(self.solo_tenant, CustodyMode.DIRECT)

        result = held.apply_custody_switch(row)
        self.assertEqual(result["result"], "waiting")
        row.refresh_from_db()
        self.assertIn("Main Branch has ₦394.00 held", row.pending_note)
        self.assertEqual(custody.custody_mode(self.solo_tenant), CustodyMode.HELD)
        final = HeldSettlement.objects.get(branch=self.solo_main)
        self.assertTrue(final.final)  # Includes today's payment.

        payout = self.dispatch(final.batch)
        self.confirm(payout, amount=39_400)
        result = held.apply_custody_switch(row)
        self.assertEqual(result["result"], "switched")
        row.refresh_from_db()
        self.assertEqual((row.mode, row.pending_mode, row.pending_note), (CustodyMode.DIRECT, "", ""))
        self.assertEqual(custody.custody_mode(self.solo_tenant), CustodyMode.DIRECT)

        old.refresh_from_db()
        self.assertEqual(old.status, VirtualAccountStatus.RETIRED)
        new = old.replaced_by
        self.assertEqual((new.status, new.settlement_subaccount, new.customer_id),
                         (VirtualAccountStatus.ACTIVE, "ACCT_SOLO", self.solo_parent.pk))
        self.assertNotEqual(new.account_number, old.account_number)
        with self.assertRaises(ValidationError):
            services.set_virtual_account_status(old, status=VirtualAccountStatus.ACTIVE)

    def test_a_branch_not_ready_keeps_a_multi_branch_tenant_waiting(self):
        self.subaccount(self.ikeja_bank, "ACCT_IKJ")
        self.subaccount(self.lekki_bank, "ACCT_LEK")
        row = self.pending(self.tenant, CustodyMode.DIRECT)
        result = held.apply_custody_switch(row)
        self.assertEqual(result["result"], "waiting")
        self.assertIn("not set up with the payment provider: Yaba Branch", result["reasons"][0])

    def test_money_paid_into_a_retired_number_is_credited_and_passed_on(self):
        old = services.create_virtual_account(entity=self.solo_books, customer=self.solo_parent)
        self.subaccount(self.solo_bank, "ACCT_SOLO")
        self.direct(self.solo_tenant)
        services.reissue_virtual_account(old)
        old.refresh_from_db()
        self.assertEqual(old.status, VirtualAccountStatus.RETIRED)

        intent, _ = services.record_virtual_account_deposit(
            virtual_account=old, reference="PSTK-LATE-1", amount=25_000)
        self.fake.forced_status[intent.reference] = CollectionStatus.SUCCEEDED
        self.fake.forced_amount[intent.reference] = 25_000
        self.fake.forced_fee[intent.reference] = 375
        intent = services.confirm_collection(intent)
        self.assertEqual((intent.status, intent.customer_id, intent.held_by_platform),
                         (CollectionStatus.SUCCEEDED, self.solo_parent.pk, True))
        self.assertEqual(held.held_balance(self.solo_main.pk), 24_625)

        self.yesterday(intent)
        summary = held.run_settlements()
        run = HeldSettlement.objects.get(pk=summary["built"][0])
        self.assertEqual((run.branch_id, run.amount), (self.solo_main.pk, 24_625))

    def test_direct_to_held_takes_effect_on_its_day(self):
        PaymentCustodySettings.objects.update_or_create(
            tenant=self.tenant, defaults={"mode": CustodyMode.DIRECT})
        row = self.pending(self.tenant, CustodyMode.HELD)
        self.assertEqual(custody.custody_mode(self.tenant), CustodyMode.HELD)
        self.assertEqual(held.apply_custody_switch(row)["result"], "switched")
        row.refresh_from_db()
        self.assertEqual(row.mode, CustodyMode.HELD)
        intent = services.initiate_collection(entity=self.books, amount=1_000, customer=self.adeyemi)
        self.assertNotIn(intent.reference, self.fake.subaccounts_named)


class HeldSettlementEndpointTests(_HeldFixture):
    """Settlement runs are read within the caller's branches."""

    def client_for(self, *keys, branch=None, tenant=None):
        tenant = tenant or self.tenant
        n = next(_people)
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"held-{n}@corona.test"), *keys,
            tenant=tenant, role_key=f"held-{n}", branch=branch,
        ))

    def test_a_branch_reader_sees_only_its_branchs_settlements(self):
        lekki = self.paid(self.books, self.adeyemi, 180_000)
        ikeja = self.paid(self.books, self.okafor, 100_000)
        self.yesterday(lekki, ikeja)
        held.run_settlements()
        url = f"/v1/payments/held-settlements/?entity={self.books.code}"

        self.assertEqual(self.client_for("payments.payout.view").get(url).status_code, 403)
        reader = self.client_for("payments.report.view", branch=self.lekki)
        response = reader.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["branch_name"] for row in response.data["data"]], ["Lekki Branch"])
        self.assertEqual(response.data["data"][0]["status"], HeldSettlementStatus.PENDING)

        everyone = self.client_for("payments.report.view").get(url + "&status=PENDING")
        self.assertEqual(len(everyone.data["data"]), 2)
        none = self.client_for("payments.report.view", branch=self.yaba).get(url)
        self.assertEqual(none.data["data"], [])

    def test_a_settlement_is_a_transfer_in_both_feeds_not_money_spent(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.yesterday(intent)
        run, _ = held.build_settlement(self.books, self.lekki.pk)
        with patch.object(services, "_validate_approved_instance", side_effect=_approval_passes):
            services.submit_payout_batch(run.batch, approved_instance=object())
        payout = run.batch.instructions.get()
        self.fake.forced_status[payout.reference] = PayoutStatus.PAID
        services.confirm_payout(payout)

        school = self.client_for("payments.report.view", branch=self.lekki)
        rows = school.get(f"/v1/payments/movements/?entity={self.books.code}").data["data"]
        transfer = [row for row in rows if row["kind"] == "settlement"]
        self.assertEqual([(r["direction"], r["amount"], r["status"]) for r in transfer],
                         [("transfer", 178_000, HeldSettlementStatus.PAID)])
        self.assertEqual(school.get(
            f"/v1/payments/movements/?entity={self.books.code}&direction=out").data["data"], [])
        summary = school.get(f"/v1/payments/movements/summary/?entity={self.books.code}").data["data"]
        self.assertEqual((summary["out7d"]["kobo"], summary["transfers7d"]["kobo"]), (0, 178_000))

        codex = self.client_for("payments.report.view", tenant=self.platform.tenant)
        codex_rows = codex.get(f"/v1/payments/movements/?entity={self.platform.code}").data["data"]
        self.assertEqual([(r["kind"], r["direction"]) for r in codex_rows],
                         [("settlement", "transfer")])
        codex_summary = codex.get(
            f"/v1/payments/movements/summary/?entity={self.platform.code}").data["data"]
        self.assertEqual((codex_summary["out7d"]["kobo"], codex_summary["transfers7d"]["kobo"]),
                         (0, 178_000))


class HeldReconciliationTests(_HeldFixture):
    """Each day the platform's books are compared with what Paystack says it holds."""

    def setUp(self):
        super().setUp()
        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def open_incidents(self):
        from vs_health.models import Incident

        return Incident.objects.filter(fault_key=held_reconciliation.FAULT_KEY).exclude(
            status=Incident.Status.RESOLVED)

    def test_agrees_disagrees_raises_once_then_agrees_again_and_resolves(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        self.fake.balances["NGN"] = 217_400

        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.provider_balance, row.books_balance, row.held_total, row.difference,
                          row.agrees), (217_400, 217_400, 217_400, 0, True))
        self.assertFalse(self.open_incidents().exists())

        self.fake.balances["NGN"] = 217_350  # A N0.50 fee nobody booked.
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (-50, False))
        incident = self.open_incidents().get()
        self.assertEqual(row.incident_code, incident.code)
        self.assertIn("a difference of", incident.summary)
        again = held_reconciliation.reconcile_held_ledger()  # The same day, run again.
        self.assertEqual((again.pk, again.incident_code), (row.pk, incident.code))
        self.assertEqual(self.open_incidents().count(), 1)
        self.assertEqual(self.notify.call_count, 1)  # Operators are told once.

        self.fake.balances["NGN"] = 217_400
        row = held_reconciliation.reconcile_held_ledger()
        self.assertTrue(row.agrees)
        self.assertFalse(self.open_incidents().exists())
        self.assertEqual(HeldReconciliation.objects.count(), 1)  # One row per day.

    def test_a_difference_within_the_tolerance_agrees(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.balances["NGN"] = 177_950
        with patch.object(held_reconciliation, "tolerance", return_value=100):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.tolerance, row.agrees), (-50, 100, True))

    def test_a_movement_not_yet_posted_is_a_disagreement(self):
        with patch.object(held, "platform_books", return_value=None):
            self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.balances["NGN"] = 178_000
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.provider_account, row.held_total, row.agrees), (0, 178_000, False))
        self.assertIn("not yet posted", self.open_incidents().get().summary)

    def test_a_provider_that_cannot_answer_is_recorded_not_raised(self):
        from .exceptions import ProviderError

        with patch.object(self.fake, "available_balance",
                          side_effect=ProviderError("Paystack is down.", provider="PAYSTACK")):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.provider_balance, row.difference, row.agrees, row.error),
                         (None, None, False, "Paystack is down."))
        self.assertFalse(self.open_incidents().exists())

    def test_platform_staff_read_the_checks_and_nobody_else(self):
        self.fake.balances["NGN"] = 0
        held_reconciliation.reconcile_held_ledger()
        codex = self.platform.tenant
        staff = self.grant(self.user_for(codex, "recon@codex.test"),
                           "payments.platform_settlement.view", tenant=codex, role_key="recon")
        response = TenantAPIClient(user=staff).get("/v1/payments/platform/held-reconciliations/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["agrees"] for row in response.data["data"]], [True])
        bursar = self.grant(self.user_for(self.tenant, "recon@corona.test"),
                            "payments.platform_settlement.view", tenant=self.tenant,
                            role_key="recon-corona")
        self.assertEqual(TenantAPIClient(user=bursar).get(
            "/v1/payments/platform/held-reconciliations/").status_code, 403)
