"""Finding money the provider settled that never reached the books.

A collection or payout is confirmed when its webhook is processed or when
somebody presses verify. Both can fail to happen. The provider's webhook can go
to an old address, the one task that processes a stored event can be lost to a
worker restart after the provider already had its 200 (and so never resends),
or the parent can close the tab before the redirect. Each leaves real money
outside the books, with nothing looking for it: the unbooked-money alarms count
only events that arrived and failed.

:func:`recover_unconfirmed_payments` is what looks. It runs on beat, asks the
provider, and books what the provider confirms, through the same idempotent
confirm services the webhook uses, so it can never book twice. Three passes, in
this order:

1. **Stored events still RECEIVED** after :data:`STALE_EVENT_AFTER` are
   processed again. This comes first because a virtual-account deposit has no
   collection until its event is processed, so no later pass could find it.
2. **Collections not yet booked**: PENDING and PROCESSING for
   :data:`COLLECTION_GIVE_UP_AFTER`, and FAILED or ABANDONED (with a provider
   reference) for :data:`PROVISIONAL_RECHECK_FOR`, since a parent can finish
   paying on a checkout the provider earlier called abandoned.
3. **Payouts still PROCESSING**, including those whose send timed out with the
   outcome unknown. There is no give-up age: money may have left, and a payout
   that stays PROCESSING keeps its bill open for somebody to pay a second time.

Each row is re-asked on a backoff measured from its age (:func:`reverify_interval`):
every half hour at first, hourly after six hours, daily after two days. The
row's ``updated_at`` is the record of when it was last asked; the confirm
services save on every answer, and a failed attempt is stamped here, so a row
the provider cannot answer for is not asked again on every run.

A receipt booked here is dated the day the provider says the payer paid, not
the day it was booked, whenever that day's period is still open (see
:func:`vs_payments.services._booking_date`). Booking a record also closes its
stale stored events (:func:`vs_payments.webhooks.settle_events_for`), so the
unbooked-money digest stops reporting money that is now in the books.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .constants import (
    COLLECTION_PROVISIONAL_FAILURES,
    CollectionStatus,
    PayoutStatus,
    WebhookStatus,
)

logger = logging.getLogger("vs_payments.recovery")

#: How long a row is left to its webhook before the sweep asks the provider itself.
REVERIFY_AFTER = timedelta(minutes=30)

#: How long a stored event may sit RECEIVED before its lost task is re-run.
STALE_EVENT_AFTER = timedelta(minutes=15)

#: Age after which an unpaid checkout is taken as never going to be paid.
COLLECTION_GIVE_UP_AFTER = timedelta(days=30)

#: How long a FAILED or ABANDONED collection is still re-checked for a late success.
PROVISIONAL_RECHECK_FOR = timedelta(days=2)

#: Most rows of each kind asked about in one run; the rest wait for the next.
BATCH_LIMIT = 200

#: Most rows of each kind read per run. Rows come least recently asked first, so
#: the ones not yet due (see :func:`reverify_interval`) sit behind those that are.
_SCAN_LIMIT = BATCH_LIMIT * 5


def reverify_interval(age: timedelta) -> timedelta:
    """How long to wait between two provider checks of a row ``age`` old."""
    if age < timedelta(hours=6):
        return timedelta(minutes=30)
    if age < timedelta(days=2):
        return timedelta(hours=1)
    return timedelta(days=1)


def _due(row, now) -> bool:
    """True when ``row`` was last asked about at least one backoff interval ago."""
    return now - row.updated_at >= reverify_interval(now - row.created_at)


def recover_unconfirmed_payments(*, now=None) -> dict:
    """Re-run lost webhook events and re-verify unconfirmed collections and payouts.

    Returns counts: ``events`` re-run, ``collections``/``payouts`` asked about,
    ``collections_booked``/``payouts_booked``, and ``failures``. One row's
    failure is logged and counted and never stops the rest.
    """
    now = now or timezone.now()
    summary = {
        "events": 0, "collections": 0, "collections_booked": 0,
        "payouts": 0, "payouts_booked": 0, "failures": 0,
    }
    _rerun_stale_events(now, summary)
    _reverify_collections(now, summary)
    _reverify_payouts(now, summary)
    return summary


def _rerun_stale_events(now, summary) -> None:
    """Process stored events whose processing task never ran."""
    from . import webhooks
    from .models import WebhookEvent

    stale = (
        WebhookEvent.objects
        .filter(status=WebhookStatus.RECEIVED, created_at__lte=now - STALE_EVENT_AFTER)
        .order_by("created_at")
        .values_list("pk", flat=True)[:BATCH_LIMIT]
    )
    for event_id in list(stale):
        try:
            webhooks.process_stored_event(event_id)  # Idempotent; records its own failure.
            summary["events"] += 1
        except Exception:  # noqa: BLE001 - one event must not stop the sweep
            logger.exception("recover_unconfirmed_payments: event %s failed.", event_id)
            summary["failures"] += 1


def _reverify_collections(now, summary) -> None:
    """Ask the provider about collections that have not booked, and book what it confirms."""
    from . import services, webhooks
    from .models import CollectionIntent

    candidates = (
        CollectionIntent.objects
        .filter(
            Q(status__in=(CollectionStatus.PENDING, CollectionStatus.PROCESSING),
              created_at__gte=now - COLLECTION_GIVE_UP_AFTER)
            | (Q(status__in=COLLECTION_PROVISIONAL_FAILURES,
                 created_at__gte=now - PROVISIONAL_RECHECK_FOR)
               & ~Q(provider_reference="")),
            payment__isnull=True,
            created_at__lte=now - REVERIFY_AFTER,
            updated_at__lte=now - REVERIFY_AFTER,
        )
        .order_by("updated_at")
    )
    asked = 0
    for intent in candidates[:_SCAN_LIMIT]:
        if asked >= BATCH_LIMIT:
            break
        if not _due(intent, now):
            continue
        asked += 1
        try:
            intent = services.confirm_collection(intent)
        except Exception:  # noqa: BLE001 - one collection must not stop the sweep
            logger.exception(
                "recover_unconfirmed_payments: collection %s could not be confirmed.",
                intent.reference,
            )
            summary["failures"] += 1
            CollectionIntent.objects.filter(pk=intent.pk).update(updated_at=now)
            continue
        if intent.status == CollectionStatus.SUCCEEDED:
            summary["collections_booked"] += 1
            webhooks.settle_events_for(collection=intent)
    summary["collections"] += asked


def _reverify_payouts(now, summary) -> None:
    """Ask the provider about payouts still in flight, and book what it confirms paid."""
    from . import services, webhooks
    from .models import PayoutInstruction

    candidates = (
        PayoutInstruction.objects
        .filter(status=PayoutStatus.PROCESSING, updated_at__lte=now - REVERIFY_AFTER)
        .order_by("updated_at")
    )
    asked = 0
    for payout in candidates[:_SCAN_LIMIT]:
        if asked >= BATCH_LIMIT:
            break
        if not _due(payout, now):
            continue
        asked += 1
        try:
            payout = services.confirm_payout(payout)
        except Exception:  # noqa: BLE001 - one payout must not stop the sweep
            logger.exception(
                "recover_unconfirmed_payments: payout %s could not be confirmed.",
                payout.reference,
            )
            summary["failures"] += 1
            PayoutInstruction.objects.filter(pk=payout.pk).update(updated_at=now)
            continue
        if payout.status == PayoutStatus.PAID:
            summary["payouts_booked"] += 1
            webhooks.settle_events_for(payout=payout)
    summary["payouts"] += asked
