"""A refund of an online payment goes back to the payer through the provider.

Mrs Adeyemi paid Corona's Lekki branch N180,000 online and the provider kept N2,000,
so the platform holds N178,000 for Lekki. Her son is withdrawn and the bursar raises
a N100,000 refund, method Online, and ties it to that payment. Posting it reserves
N100,000 of Lekki's held money and asks the provider; when the provider reports the
money returned, Lekki's books record Dr customer credit, Cr gateway clearing. If the
provider fails it, the reservation comes back and the bursar tries again or pays from
Lekki's own bank. Ikeja's money is never touched, and a school that settles directly
to its banks never refunds this way.
"""
from __future__ import annotations

import datetime
import itertools
from types import SimpleNamespace
from unittest.mock import patch

from rest_framework.exceptions import ValidationError

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_finance.constants import DocumentStatus, PaymentMethod
from vs_finance.credit_notes import post_refund
from vs_finance.exceptions import PostingError
from vs_finance.models import Account, Refund
from vs_finance.voids import void_refund

from . import held, held_reconciliation, recovery, refunds, webhooks
from .constants import (
    HeldMovementKind,
    PaymentAuditAction,
    ProviderRefundStatus,
)
from .custody import OnlineRefundsNotOfferedError
from .exceptions import DuplicateWebhookError, PaymentStateError
from .held import InsufficientHeldFundsError
from .models import HeldBalance, HeldMovement, PaymentEvent, ProviderRefund
from .tests_custody_held import _HeldFixture

_people = itertools.count(1)


class _RefundFixture(_HeldFixture):
    def refund(self, customer, branch, amount, *, entity=None, method=PaymentMethod.ONLINE):
        entity = entity or self.books
        return Refund.objects.create(
            entity=entity, customer=customer, branch=branch, method=method, amount=amount,
            refund_date=tenant_today(entity.tenant),
        )

    def post(self, refund):
        """Post the refund and run what is queued for after the commit."""
        with self.captureOnCommitCallbacks(execute=True):
            return post_refund(refund)

    def tie(self, refund, intent, entity=None):
        return refunds.link_refund(entity=entity or self.books, refund=refund, collection=intent)

    def settle(self, link, status="PROCESSED"):
        self.fake.forced_refund_status[link.transaction_reference] = status
        return refunds.confirm_provider_refund(link)

    def sent_refund(self, customer, branch, intent, amount, entity=None):
        """A refund tied, posted and with the provider; returns (finance refund, link)."""
        refund = self.refund(customer, branch, amount, entity=entity)
        link = self.tie(refund, intent, entity)
        self.post(refund)
        link.refresh_from_db()
        return refund, link


class ProviderRefundMoneyTests(_RefundFixture):
    def test_a_partial_refund_reserves_then_books_to_clearing_when_the_provider_returns_it(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)
        link = self.tie(refund, intent)

        self.post(refund)

        refund.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.APPROVED)  # Not booked until it is returned.
        self.assertIsNone(refund.journal_id)
        self.assertEqual((link.status, link.provider_status), (ProviderRefundStatus.PROCESSING, "pending"))
        self.assertEqual(link.provider_reference, "FAKE-RF-1")
        self.assertEqual(self.fake.refunds_created[0]["amount"], 100_000)
        self.assertEqual(self.fake.refunds_created[0]["transaction_reference"], intent.reference)
        self.assertEqual(held.held_balance(self.lekki.pk), 78_000)  # Reserved when sent.
        movement = HeldMovement.objects.get(provider_refund=link, kind=HeldMovementKind.REFUND)
        self.assertEqual((movement.amount, movement.branch_id), (-100_000, self.lekki.pk))
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 180_000)

        self.settle(link)

        refund.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual((refund.status, link.status), (DocumentStatus.POSTED, ProviderRefundStatus.PROCESSED))
        credit = Account.objects.get(entity=self.books, code="2140")
        self.assertEqual(self.balance(credit, self.lekki), -80_000)  # 180,000 received, 100,000 back.
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 80_000)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), 0)  # Not a bank refund.
        self.assertEqual(refund.journal.branch_id, self.lekki.pk)
        self.assertEqual(held.held_balance(self.lekki.pk), 78_000)  # The reduction stands.
        self.assert_books_agree()
        self.assertTrue(PaymentEvent.objects.filter(
            action=PaymentAuditAction.PROVIDER_REFUND_PROCESSED, reference=intent.reference).exists())

    def test_the_held_ledger_still_agrees_with_the_provider_after_a_refund(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        intent = self.paid(self.books, self.adeyemi, 50_000, fee=750)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 30_000)
        self.settle(link)
        self.fake.balances["NGN"] = 178_000 + 49_250 - 30_000  # What the provider now holds.

        with patch.object(held_reconciliation, "_notify_operators", return_value=1):
            row = held_reconciliation.reconcile_held_ledger()

        self.assertEqual((row.provider_balance, row.books_balance, row.held_total, row.agrees),
                         (197_250, 197_250, 197_250, True))

    def test_the_provider_failing_it_releases_the_reservation_and_reopens_the_draft(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)

        self.settle(link, "FAILED")

        refund.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual((link.status, refund.status), (ProviderRefundStatus.FAILED, DocumentStatus.DRAFT))
        self.assertTrue(link.failure_reason)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)
        self.assertIsNone(refund.journal_id)
        self.assert_books_agree()

        # The bursar tries again: the failed attempt no longer counts against the payment.
        again = self.tie(refund, intent)
        self.post(refund)
        again.refresh_from_db()
        self.assertEqual(again.status, ProviderRefundStatus.PROCESSING)
        self.assertEqual(held.held_balance(self.lekki.pk), 78_000)

    def test_a_failed_refund_can_be_paid_from_the_branchs_own_bank_instead(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.settle(link, "FAILED")

        refunds.pay_from_bank(link, method=PaymentMethod.BANK_TRANSFER, bank_account=self.lekki_bank)
        refund.refresh_from_db()
        post_refund(refund)

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), -100_000)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)  # No held money used.
        self.assertFalse(ProviderRefund.objects.filter(
            status__in=(ProviderRefundStatus.LINKED, ProviderRefundStatus.PROCESSING)).exists())

    def test_a_refund_beyond_the_branchs_held_money_is_refused_and_nothing_is_sent(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)  # 178,000 reaches the balance.
        refund = self.refund(self.adeyemi, self.lekki, 180_000)
        self.tie(refund, intent)

        with self.assertRaises(InsufficientHeldFundsError) as caught:
            self.post(refund)

        self.assertIn("held by the platform", str(caught.exception))
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.DRAFT)
        self.assertEqual(self.fake.refunds_created, [])
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_two_refunds_cannot_return_more_than_the_payment(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=0)
        first = self.refund(self.adeyemi, self.lekki, 100_000)
        self.tie(first, intent)
        second = self.refund(self.adeyemi, self.lekki, 100_000)

        with self.assertRaises(ValidationError) as caught:
            self.tie(second, intent)

        self.assertIn("80", str(caught.exception.detail["amount"]))
        self.assertEqual(refunds.refundable_through_provider(intent), 80_000)

    def test_a_refund_posted_while_another_has_spent_the_held_money_is_refused(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        first = self.refund(self.adeyemi, self.lekki, 100_000)
        second = self.refund(self.adeyemi, self.lekki, 78_000)
        self.tie(first, intent)
        self.tie(second, intent)
        self.post(first)
        HeldBalance.objects.filter(branch=self.lekki).update(balance=77_999)  # A payout spent some.

        with self.assertRaises(InsufficientHeldFundsError):
            self.post(second)

    def test_a_refund_returned_cannot_be_voided_back(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.settle(link)

        with self.assertRaises(PostingError) as caught:
            void_refund(refund)

        self.assertIn("through the payment provider", str(caught.exception))

    def test_a_one_branch_school_refunds_through_its_only_branch(self):
        intent = self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)
        refund, link = self.sent_refund(
            self.solo_parent, self.solo_main, intent, 10_000, entity=self.solo_books)

        self.assertEqual(held.held_balance(self.solo_main.pk), 29_400)
        self.settle(link)
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertEqual(self.balance(self.clearing(self.solo_books)), 30_000)

    def test_a_two_branch_school_keeps_each_branchs_money_apart(self):
        lekki = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        ikeja = self.paid(self.books, self.okafor, 100_000, fee=1_500)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, lekki, 100_000)
        self.settle(link)

        self.assertEqual(held.held_balance(self.lekki.pk), 78_000)
        self.assertEqual(held.held_balance(self.ikeja.pk), 98_500)
        self.assertEqual(self.balance(self.clearing(self.books), self.ikeja), 100_000)
        self.assertEqual(self.balance(self.clearing(self.books), self.lekki), 80_000)
        self.assert_books_agree()
        self.assertIsNotNone(ikeja)


class ProviderRefundRulesTests(_RefundFixture):
    def test_a_direct_tenant_never_refunds_online(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)
        self.settle_directly()

        with self.assertRaises(OnlineRefundsNotOfferedError):
            self.tie(refund, intent)
        self.assertFalse(ProviderRefund.objects.exists())

    def test_the_payment_must_be_the_same_customer_and_branch(self):
        lekki = self.paid(self.books, self.adeyemi, 180_000, fee=0)
        ikeja = self.paid(self.books, self.okafor, 100_000, fee=0)
        refund = self.refund(self.adeyemi, self.lekki, 10_000)

        with self.assertRaises(ValidationError) as other_customer:
            self.tie(refund, ikeja)
        self.assertIn("different customer", str(other_customer.exception.detail))

        elsewhere = self.refund(self.adeyemi, self.ikeja, 10_000)
        with self.assertRaises(ValidationError) as other_branch:
            self.tie(elsewhere, lekki)
        self.assertIn("different branch", str(other_branch.exception.detail))

    def test_only_a_confirmed_held_payment_of_the_refunds_own_books_can_be_returned(self):
        pending = self.started_payment()
        refund = self.refund(self.adeyemi, self.lekki, 1_000)
        with self.assertRaises(PaymentStateError):
            self.tie(refund, pending)

        self.settle_directly()
        direct = self.paid(self.books, self.adeyemi, 5_000)
        self.assertFalse(direct.held_by_platform)

    def started_payment(self):
        from . import services

        return services.initiate_collection(entity=self.books, amount=5_000, customer=self.adeyemi)

    def test_only_an_online_draft_can_be_tied(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=0)
        bank = self.refund(self.adeyemi, self.lekki, 1_000, method=PaymentMethod.BANK_TRANSFER)
        with self.assertRaises(ValidationError):
            self.tie(bank, intent)

    def test_an_online_refund_not_tied_to_a_payment_posts_to_the_bank_as_before(self):
        self.paid(self.books, self.adeyemi, 180_000, fee=0)
        refund = self.refund(self.adeyemi, self.lekki, 1_000)
        refund.bank_account = self.lekki_bank
        refund.save(update_fields=["bank_account"])

        post_refund(refund)

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        self.assertEqual(self.balance(self.lekki_bank.gl_account), -1_000)

    def test_a_provider_rejection_fails_the_refund_without_leaving_money_reserved(self):
        from .exceptions import ProviderError

        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.refund_error = ProviderError("Transaction already reversed", provider="PAYSTACK",
                                               provider_code="400")
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)

        refund.refresh_from_db()
        self.assertEqual((link.status, refund.status), (ProviderRefundStatus.FAILED, DocumentStatus.DRAFT))
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_provider_that_cannot_be_reached_leaves_the_refund_in_flight(self):
        from .exceptions import ProviderError

        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.fake.refund_error = ProviderError("timed out", provider="PAYSTACK")
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)

        self.assertEqual(link.status, ProviderRefundStatus.PROCESSING)
        self.assertIsNotNone(link.sent_at)
        self.assertEqual(held.held_balance(self.lekki.pk), 78_000)

    def test_a_second_send_never_asks_the_provider_twice(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        _, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        refunds.send_provider_refund(link.pk)
        self.assertEqual(len(self.fake.refunds_created), 1)


class ProviderRefundEventTests(_RefundFixture):
    def deliver(self, intent, event, status, amount=100_000):
        body, headers = self.fake.build_webhook(
            event=event, reference=intent.reference, status=status, amount=amount)
        with patch("vs_payments.alerts._notify", return_value=["n"]) as notify:
            stored = webhooks.ingest_webhook(provider="PAYSTACK", raw_body=body, headers=headers)
            webhooks.process_stored_event(stored.pk)
        return notify, stored

    def test_a_processed_event_re_verifies_then_books_once_and_alerts_nobody(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.fake.forced_refund_status[intent.reference] = "PROCESSED"

        notify, stored = self.deliver(intent, "refund.processed", "processed")
        with self.assertRaises(DuplicateWebhookError):  # A re-delivery is not processed again.
            self.deliver(intent, "refund.processed", "processed")
        # An event of another id about the same refund is still ours, and books nothing twice.
        self.assertEqual(refunds.confirm_refunds_of(intent, SimpleNamespace(amount=100_000)), 1)

        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)
        stored.refresh_from_db()
        self.assertEqual(stored.collection_id, intent.pk)
        notify.assert_not_called()
        self.assertEqual(self.balance(Account.objects.get(entity=self.books, code="2140"), self.lekki), -80_000)
        self.assertFalse(PaymentEvent.objects.filter(
            action=PaymentAuditAction.PROVIDER_DISPUTE_RECEIVED).exists())

    def test_an_event_alone_does_not_book_what_the_provider_does_not_confirm(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)

        self.deliver(intent, "refund.processed", "processed")  # The provider still says pending.

        refund.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual((refund.status, link.status),
                         (DocumentStatus.APPROVED, ProviderRefundStatus.PROCESSING))

    def test_a_failed_event_releases_the_reservation(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.fake.forced_refund_status[intent.reference] = "FAILED"

        self.deliver(intent, "refund.failed", "failed")

        link.refresh_from_db()
        self.assertEqual(link.status, ProviderRefundStatus.FAILED)
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_a_refund_made_in_the_providers_dashboard_is_recorded_and_raised_only(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)

        notify, _ = self.deliver(intent, "refund.processed", "processed")

        notify.assert_called()
        self.assertTrue(PaymentEvent.objects.filter(
            action=PaymentAuditAction.PROVIDER_DISPUTE_RECEIVED, reference=intent.reference).exists())
        self.assertFalse(ProviderRefund.objects.exists())
        self.assertEqual(held.held_balance(self.lekki.pk), 178_000)

    def test_the_sweep_asks_about_refunds_stuck_in_flight(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.fake.forced_refund_status[intent.reference] = "PROCESSED"

        summary = recovery.recover_unconfirmed_payments(
            now=link.updated_at + datetime.timedelta(hours=3))

        refund.refresh_from_db()
        self.assertEqual((summary["refunds"], summary["refunds_settled"]), (1, 1))
        self.assertEqual(refund.status, DocumentStatus.POSTED)

    def test_the_sweep_sends_a_posted_refund_whose_hand_off_was_lost(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)
        link = self.tie(refund, intent)
        post_refund(refund)  # No on-commit callback is run: the enqueue is lost.
        self.assertEqual(self.fake.refunds_created, [])

        recovery.recover_unconfirmed_payments(
            now=ProviderRefund.objects.get(pk=link.pk).updated_at + datetime.timedelta(hours=1))

        self.assertEqual(len(self.fake.refunds_created), 1)


class ProviderRefundApiTests(_RefundFixture):
    KEYS = ("payments.refund.create", "payments.refund.view", "payments.collection.view")

    def clerk(self, *keys, tenant=None, branch=None):
        n = next(_people)
        tenant = tenant or self.tenant
        return TenantAPIClient(user=self.grant(
            self.user_for(tenant, f"refund-{n}@corona.test"), *(keys or self.KEYS),
            tenant=tenant, role_key=f"refund-{n}", branch=branch))

    def tie_via_api(self, client, refund, intent, entity=None):
        return client.post(f"/v1/payments/refunds/?entity={(entity or self.books).code}",
                           {"refund": refund.pk, "collection": intent.pk}, format="json")

    def test_a_clerk_ties_a_refund_to_the_payment_for_their_branch(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)

        response = self.tie_via_api(self.clerk(branch=self.lekki), refund, intent)

        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual((data["status"], data["amount"], data["collection_id"], data["branch"]),
                         ("LINKED", 100_000, intent.pk, self.lekki.pk))
        self.assertEqual(data["collection_reference"], intent.reference)

    def test_without_the_permission_it_is_refused(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)

        for keys in (("payments.refund.view",), ("finance.refund.create",)):
            with self.subTest(keys=keys):
                response = self.tie_via_api(self.clerk(*keys, branch=self.lekki), refund, intent)
                self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(ProviderRefund.objects.exists())

    def test_another_tenants_user_reaches_nothing(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        rival = self.clerk(tenant=self.rival_tenant)

        for response in (
            self.tie_via_api(rival, refund, intent),
            rival.get(f"/v1/payments/refunds/{link.pk}/?entity={self.books.code}"),
            rival.get(f"/v1/payments/refunds/?entity={self.books.code}"),
        ):
            self.assertIn(response.status_code, (400, 403, 404), response.data)

    def test_a_branch_clerk_neither_ties_nor_reads_another_branchs_refund(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        ikeja = self.clerk(branch=self.ikeja)

        self.assertEqual(ikeja.get(f"/v1/payments/refunds/{link.pk}/?entity={self.books.code}")
                         .status_code, 404)
        self.assertEqual(ikeja.get(f"/v1/payments/refunds/?entity={self.books.code}")
                         .data["data"], [])
        elsewhere = self.refund(self.adeyemi, self.lekki, 5_000)
        self.assertEqual(self.tie_via_api(ikeja, elsewhere, intent).status_code, 400)

    def test_polling_the_refund_books_what_the_provider_confirms(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund, link = self.sent_refund(self.adeyemi, self.lekki, intent, 100_000)
        self.fake.forced_refund_status[intent.reference] = "PROCESSED"

        response = self.clerk(branch=self.lekki).get(
            f"/v1/payments/refunds/{link.pk}/?verify=1&entity={self.books.code}")

        self.assertEqual(response.data["data"]["status"], "PROCESSED")
        refund.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.POSTED)

    def test_pay_from_bank_is_over_the_same_reach(self):
        intent = self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        refund = self.refund(self.adeyemi, self.lekki, 100_000)
        link = self.tie(refund, intent)
        path = f"/v1/payments/refunds/{link.pk}/pay-from-bank/?entity={self.books.code}"

        self.assertEqual(self.clerk(branch=self.ikeja).post(path, {}, format="json").status_code, 404)
        done = self.clerk(branch=self.lekki).post(
            path, {"bank_account": self.lekki_bank.pk}, format="json")
        self.assertEqual(done.status_code, 200, done.data)
        refund.refresh_from_db()
        self.assertEqual((refund.method, refund.bank_account_id),
                         (PaymentMethod.BANK_TRANSFER, self.lekki_bank.pk))
