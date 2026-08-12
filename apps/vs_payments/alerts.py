"""Telling somebody that money arrived and did not reach the books.

The failure itself is already handled well: :func:`vs_payments.webhooks.process_stored_event`
records it, marks the event FAILED, and deliberately swallows the exception so the PSP
(already acked, retries idempotent) is not handed a spurious 500. The Needs Attention
screen then shows it, and it can be replayed.

What none of that does is *tell anyone*. A screen is somewhere you can look; it is not
something that taps you on the shoulder. So a payment that fails on a Friday evening sits
there until somebody happens to open the page, and the customer's money is missing from
the books all weekend with nobody aware.

Two alarms, deliberately different in kind:

* :func:`unbooked_digest` is a standing daily report - "you still have three of these".
  It re-sends while the backlog exists, which is correct for a standing report and is why
  it is not a per-event alert: during a month-end close *every* receipt fails, and a
  hundred alerts is worth the same as none.
* :func:`unbooked_surge` is an incident alarm. Several failures inside one window points
  at a systemic cause - a closed fiscal period, a provider change - and that is a platform
  concern, not something an individual school can fix.

Neither needs state on the event. The digest reports the current outstanding set, and the
surge only counts what arrived inside its own window, so both are stateless and a missed
run costs nothing but a late notice.
"""
from __future__ import annotations

import logging
from collections import Counter

from django.db.models import Q
from django.utils import timezone

from .constants import WebhookStatus

logger = logging.getLogger("vs_payments.alerts")

#: How many failures inside one window mean "something is broken" rather than
#: "one payment went wrong". Deliberately low: unbooked money is expensive.
SURGE_THRESHOLD = 3

#: The window the surge alarm looks back over. Matches its beat interval, so a
#: continuing outage re-alarms once per window and a resolved one goes quiet by itself.
SURGE_WINDOW_MINUTES = 60

#: Who gets told. The digest goes to whoever can act on it in that entity; the surge
#: goes to platform staff, who own the causes that break every entity at once.
DIGEST_PERMISSION = "payments.webhook.view"
SURGE_PERMISSION = "payments.unattributed_webhook.view"

#: Events that mean money moved at the provider and we did not record it.
UNBOOKED_STATUSES = (WebhookStatus.FAILED, WebhookStatus.IGNORED)


# Describe the most common reason in a set of events.
def _leading_reason(events) -> str:
    """The error most of these events share, or "" when they have none in common.

    The reason is already stored on every event, and naming it is the difference
    between a message someone can act on ("no fiscal period is open") and one they
    cannot ("3 webhook failures").
    """
    reasons = Counter(e.error.strip() for e in events if e.error and e.error.strip())
    if not reasons:
        return ""
    reason, _count = reasons.most_common(1)[0]
    return reason


# Total the money an unbooked set represents.
def _total_amount(events) -> int:
    """Kobo across the events we can price.

    An event that matched nothing local has no amount to read, so the total is a
    floor rather than the whole exposure. The message says so.
    """
    total = 0
    for event in events:
        target = event.collection or event.payout
        if target is not None:
            total += int(target.amount)
    return total


# Find who should hear about an entity's unbooked money.
def _recipients(tenant, permission_key, scope):
    """Users holding ``permission_key`` in ``scope``, or [] if RBAC is absent.

    This asks RBAC directly rather than going through the workflow engine. The
    question here is "who may act on this money", which is a permission, not
    "who approves this document", which is now a role, a group, or a rule. The
    workflow engine no longer resolves permissions, and routing an alert through
    an approval concept would have coupled the two for no reason.

    ``scope`` is retained for the caller's vocabulary. Recipients are resolved
    tenant-wide (``branch=None``), exactly as before.
    """
    try:
        from vs_rbac.evaluator import resolve_users_with_permission
    except ImportError:  # pragma: no cover - rbac is always installed in practice
        logger.warning("vs_rbac unavailable; cannot resolve alert recipients.")
        return []
    return list(resolve_users_with_permission(
        tenant=tenant, branch=None, permission_key=permission_key,
    ))


# Send one notification, never letting delivery break the caller.
def _notify(event_key, *, context, recipients, tenant=None, school=None):
    """Dispatch through vs_notifications, logging rather than raising on failure.

    An alarm that crashes the task it rides on would take down the next entity's
    alarm with it, which is the opposite of what an alarm is for.
    """
    if not recipients:
        logger.warning(
            "%s: nobody holds the permission needed to receive this alert.", event_key)
        return []
    try:
        from vs_notifications.notify import send_notification

        return send_notification(
            event_key, context=context, recipients=recipients,
            tenant=tenant, school=school,
        )
    except Exception:  # noqa: BLE001 - delivery must never break the sweep
        logger.exception("%s: could not dispatch alert.", event_key)
        return []


# Report every entity's outstanding unbooked receipts.
def unbooked_digest():
    """Tell each entity what of its money is sitting outside the books.

    One message per entity, not per event. Returns a
    ``{"entities": N, "events": N, "notified": N}`` summary; every entity is wrapped
    so one bad tenant cannot abort the sweep.
    """
    from vs_finance.models import LedgerEntity

    from .models import WebhookEvent

    summary = {"entities": 0, "events": 0, "notified": 0}
    outstanding = (
        WebhookEvent.objects
        .filter(status__in=UNBOOKED_STATUSES)
        .select_related("collection__entity", "payout__entity")
    )
    by_entity: dict[int, list] = {}
    for event in outstanding:
        target = event.collection or event.payout
        if target is None:  # Unattributable: the surge alarm and the platform screen own these.
            continue
        by_entity.setdefault(target.entity_id, []).append(event)

    if not by_entity:
        return summary

    entities = {
        entity.pk: entity for entity in
        LedgerEntity.objects.filter(pk__in=by_entity, is_active=True)
        .select_related("tenant")
    }
    for entity_id, events in by_entity.items():
        entity = entities.get(entity_id)
        if entity is None:  # Inactive or deleted entity: nobody to tell.
            continue
        try:
            summary["entities"] += 1
            summary["events"] += len(events)
            total = _total_amount(events)
            sent = _notify(
                "payments.unbooked_receipts_digest",
                context={
                    "entity_code": entity.code,
                    "entity_name": entity.name,
                    "count": len(events),
                    "total_amount": total,
                    "total_amount_naira": _naira(total),
                    "reason": _leading_reason(events),
                    "oldest": min(e.created_at for e in events).date().isoformat(),
                },
                recipients=_recipients(entity.tenant, DIGEST_PERMISSION, "SCHOOL"),
                tenant=entity.tenant,
            )
            summary["notified"] += len(sent)
        except Exception:  # noqa: BLE001 - one entity must not abort the rest
            logger.exception("unbooked_digest: entity %s failed.", entity_id)
    return summary


# Raise the alarm when bookings start failing in bulk.
def unbooked_surge(*, window_minutes=SURGE_WINDOW_MINUTES, threshold=SURGE_THRESHOLD):
    """Alarm platform staff when several events fail to book inside one window.

    Counts only what arrived inside the window, which is what makes this stateless
    and self-limiting: a continuing outage re-alarms once per window because new
    failures keep landing, and it falls silent the moment they stop. Unattributable
    events count too - they are failures of the same kind, and they are exactly the
    ones no entity-scoped screen shows.

    Returns ``{"failures": N, "alarmed": bool, "notified": N}``.
    """
    from vs_tenants.models import Tenant

    from .models import WebhookEvent

    since = timezone.now() - timezone.timedelta(minutes=window_minutes)
    recent = list(
        WebhookEvent.objects
        .filter(status__in=UNBOOKED_STATUSES, created_at__gte=since)
        .select_related("collection__entity", "payout__entity")
    )
    if len(recent) < threshold:
        return {"failures": len(recent), "alarmed": False, "notified": 0}

    entity_codes = sorted({
        target.entity.code
        for target in (e.collection or e.payout for e in recent)
        if target is not None
    })
    platform = Tenant.objects.filter(kind="PLATFORM").first()
    if platform is None:  # pragma: no cover - the platform tenant is seeded
        logger.error("unbooked_surge: no platform tenant to alert.")
        return {"failures": len(recent), "alarmed": True, "notified": 0}

    total = _total_amount(recent)
    sent = _notify(
        "payments.unbooked_receipts_surge",
        context={
            "count": len(recent),
            "window_minutes": window_minutes,
            "total_amount": total,
            "total_amount_naira": _naira(total),
            "reason": _leading_reason(recent),
            "entities": ", ".join(entity_codes) or "unattributed events only",
        },
        recipients=_recipients(platform, SURGE_PERMISSION, "PLATFORM"),
        tenant=platform,
    )
    return {"failures": len(recent), "alarmed": True, "notified": len(sent)}


def _naira(kobo: int) -> str:
    """Format kobo for a human, without importing finance at module load."""
    from vs_finance.money import format_naira

    return format_naira(kobo)
