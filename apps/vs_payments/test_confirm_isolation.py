"""The provider round-trip must not happen inside the booking transaction.

``confirm_collection`` and ``confirm_payout`` were each one ``@transaction.atomic``
function that took a row lock, then called Paystack from inside it, then booked.
That is a network call inside a database transaction holding a lock, and it fails
twice over when the provider is merely *slow* rather than down:

    Paystack degrades to eight-second responses on a Monday morning. Every worker
    confirming a parent's fee payment now holds a Postgres connection open for
    eight seconds, so the pool drains and the API stops answering - teachers
    cannot open a class register because a payment gateway is slow. Meanwhile
    Paystack, seeing no prompt ack, re-delivers; each re-delivery blocks on the
    lock the first one is still holding, which makes the pile-up worse exactly
    when things are already bad.

The work is now split: verify with nothing locked, then take the lock for the few
milliseconds the booking needs. These tests pin the split and, more importantly,
pin the thing that makes the split safe - the re-check under the lock. Without it,
moving the provider call out would have traded a slow system for a double-booked
one.
"""
from __future__ import annotations

from django.db import transaction
from django.test import TestCase

from vs_finance.models import Payment

from . import services
from .constants import CollectionStatus
from .models import CollectionIntent
from .tests import _PaymentsFixtureMixin


def _application_transaction_open() -> bool:
    """True when an ``atomic`` block the *application* opened is in force.

    Not ``connection.in_atomic_block``, which is always true here:
    ``django.test.TestCase`` wraps every test in its own atomic block, so the
    plain flag would report a transaction in every test whether or not the code
    under test opened one.

    The discriminator is the same one ``services._assert_no_open_transaction``
    already uses - the test harness's block carries ``_from_testcase`` - so the
    assertion in this module and the guard in production are asking the identical
    question.
    """
    blocks = transaction.get_connection().atomic_blocks
    return bool(blocks) and not getattr(blocks[-1], "_from_testcase", False)


class _TransactionSpyProvider:
    """Wraps a provider and records whether a transaction was open when called.

    That is the whole assertion: an open transaction at this moment is exactly
    the condition that made the original code hold a connection and a row lock
    for the length of a provider round-trip.
    """

    def __init__(self, inner):
        self._inner = inner
        self.in_atomic_at_call: list[bool] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def verify_collection(self, **kwargs):
        self.in_atomic_at_call.append(_application_transaction_open())
        return self._inner.verify_collection(**kwargs)

    def verify_transfer(self, **kwargs):
        self.in_atomic_at_call.append(_application_transaction_open())
        return self._inner.verify_transfer(**kwargs)


class ConfirmCollectionIsolationTests(_PaymentsFixtureMixin, TestCase):
    """Money-in: verify outside the lock, book inside it."""

    def _spy(self):
        from .providers import registry

        spy = _TransactionSpyProvider(self.fake)
        registry.register("PAYSTACK", spy)
        registry.register("FAKE", spy)
        self.addCleanup(registry.unregister)
        return spy

    def test_the_provider_is_called_with_no_transaction_open(self):
        entity, customer, _ = self.build()
        spy = self._spy()
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer,
        )
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        services.confirm_collection(intent)

        self.assertEqual(spy.in_atomic_at_call, [False])

    def test_the_receipt_is_still_booked(self):
        """Moving the call out must not have moved the work out with it."""
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=50000)
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, invoice=inv,
        )
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        intent = services.confirm_collection(intent)

        self.assertEqual(intent.status, CollectionStatus.SUCCEEDED)
        self.assertEqual(Payment.objects.get(pk=intent.payment_id).amount, 50000)

    def test_a_second_confirm_books_nothing_further(self):
        """The idempotency guarantee, unchanged.

        It used to come from the lock being held across the whole function. It now
        comes from the re-check after the lock is taken, which is the only thing
        standing between "verify outside the transaction" and booking a parent's
        fees twice.
        """
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=50000)
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, invoice=inv,
        )
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        first = services.confirm_collection(intent)
        second = services.confirm_collection(intent)

        self.assertEqual(first.payment_id, second.payment_id)
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)

    def test_a_row_settled_while_we_were_verifying_is_not_booked_again(self):
        """The race the re-check exists for, made deterministic.

        Two webhook deliveries both pass the unlocked pre-check and both verify -
        two harmless reads at the provider. Only one may book. This simulates the
        second one by settling the row *during* the provider call, which is
        precisely the window the split opened.
        """
        entity, customer, _ = self.build()
        inv = self.make_posted_invoice(entity, customer, amount=50000)
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer, invoice=inv,
        )
        self.fake.forced_status[intent.reference] = "SUCCEEDED"

        # Let the first confirm run to completion from inside the second one's
        # provider call - the other worker winning the race.
        settled = {}
        racing = {"done": False}

        class _RacingProvider(_TransactionSpyProvider):
            def verify_collection(self, **kwargs):
                # Set before recursing, not after: the nested confirm calls this
                # same method, and guarding on ``settled`` alone recurses for ever.
                if not racing["done"]:
                    racing["done"] = True
                    settled["intent"] = services.confirm_collection(
                        CollectionIntent.objects.get(pk=intent.pk)
                    )
                return super().verify_collection(**kwargs)

        from .providers import registry

        racer = _RacingProvider(self.fake)
        registry.register("PAYSTACK", racer)
        registry.register("FAKE", racer)
        self.addCleanup(registry.unregister)

        result = services.confirm_collection(
            CollectionIntent.objects.get(pk=intent.pk)
        )

        # One receipt, not two, and the loser returns the winner's row.
        self.assertEqual(Payment.objects.filter(entity=entity).count(), 1)
        self.assertEqual(result.payment_id, settled["intent"].payment_id)

    def test_an_already_terminal_intent_never_reaches_the_provider(self):
        """The unlocked pre-check earns its keep under webhook re-delivery.

        A settled row is the common case when Paystack re-delivers, and asking the
        provider about it again is a round-trip that can only return what we
        already know.
        """
        entity, customer, _ = self.build()
        intent = services.initiate_collection(
            entity=entity, amount=50000, customer=customer,
        )
        services.confirm_collection(intent, status=CollectionStatus.FAILED)

        spy = self._spy()
        services.confirm_collection(CollectionIntent.objects.get(pk=intent.pk))

        self.assertEqual(spy.in_atomic_at_call, [])
