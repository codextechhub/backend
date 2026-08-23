"""Retention for go-live request history: one year, minus one row per school.

The owner's decision is a year of history. This module implements it with one
deliberate exception, and the exception is the interesting part.

**The request that took the school live is never deleted.** It is the record of
when a school went live and who decided it, ``OnboardingProgress.go_live_at``
points at the same moment, and no later process can reconstruct it once the row
is gone. A year after activation that row is exactly as load-bearing as it was
on the day, so age is not a reason to lose it.

Everything else goes at a year: PENDING requests nobody ever reviewed, REJECTED
ones, FAILED ones, and any ACTIVATED row that approved a school which was
already live (activation is idempotent, so a second approval marks a second row
ACTIVATED without changing anything). Those are history of decisions, and the
decisions themselves survive in the audit trail, which this never touches.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from ..constants import GO_LIVE_HISTORY_RETENTION_DAYS, GoLiveStatus
from ..models import GoLiveRequest, OnboardingProgress


def activating_request_ids() -> set[int]:
    """The primary key of every request that is a record of a school going live.

    At most one per tenant. The activating row is the ACTIVATED one whose
    ``reviewed_at`` is the moment ``OnboardingProgress.go_live_at`` records:
    activation reads the clock once and writes both from it, so the two match
    exactly. Where no row matches (history written before that was true, or a
    progress row since rebuilt) the earliest ACTIVATED row is kept instead,
    because it is the only one that can have been the activation.
    """
    go_live_at = dict(
        OnboardingProgress.all_objects
        .exclude(go_live_at=None)
        .values_list("tenant_id", "go_live_at")
    )

    keep: dict[int, int] = {}
    matched: set[int] = set()
    rows = (
        GoLiveRequest.all_objects
        .filter(status=GoLiveStatus.ACTIVATED)
        # Ordered oldest first, so the fallback below is the earliest row
        # without a second query. Nulls sort together and the pk breaks ties.
        .order_by("tenant_id", "reviewed_at", "pk")
        .values_list("pk", "tenant_id", "reviewed_at")
    )
    for pk, tenant_id, reviewed_at in rows:
        stamp = go_live_at.get(tenant_id)
        if stamp is not None and reviewed_at == stamp:
            keep[tenant_id] = pk
            matched.add(tenant_id)
        elif tenant_id not in matched and tenant_id not in keep:
            keep[tenant_id] = pk
    return set(keep.values())


def purge_go_live_history(*, now=None, dry_run: bool = False) -> dict:
    """Remove go-live requests older than the retention window.

    Idempotent: the second run over the same data finds nothing, because the
    first run either deleted a row or protected it for a reason that has not
    changed. ``dry_run`` computes exactly the same sets and deletes nothing, so
    what it reports is what a real run would do.
    """
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=GO_LIVE_HISTORY_RETENTION_DAYS)

    protected = activating_request_ids()
    doomed = (
        GoLiveRequest.all_objects
        .filter(created_at__lt=cutoff)
        .exclude(pk__in=protected)
    )
    by_status = {
        row["status"]: row["n"]
        for row in doomed.values("status").annotate(n=Count("pk")).order_by("status")
    }
    doomed_ids = list(doomed.values_list("pk", flat=True))
    kept = (
        GoLiveRequest.all_objects
        .filter(created_at__lt=cutoff, pk__in=protected)
        .count()
    )

    if doomed_ids and not dry_run:
        with transaction.atomic():
            GoLiveRequest.all_objects.filter(pk__in=doomed_ids).delete()

    return {
        "cutoff": cutoff,
        "retention_days": GO_LIVE_HISTORY_RETENTION_DAYS,
        "removed": len(doomed_ids),
        "by_status": by_status,
        "kept_activating": kept,
        "dry_run": dry_run,
    }
