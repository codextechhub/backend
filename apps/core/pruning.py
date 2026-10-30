"""Records kept for a set time and then deleted: the schedule and the nightly job.

The privacy policy promises how long each kind of personal record is kept
(its Annex A, "What XVS holds"). This module is that promise in code: every
period lives in :data:`SCHEDULE`, and one nightly task walks it, deleting what
has passed its date. A period that changes in the policy changes here, once.

The last entries are erasure rather than deletion: 6 years after a pupil,
parent or member of staff leaves, their personal details are blanked and the
records the school keeps stay (:mod:`core.erasure`).

Each entry names the function in its own app that knows which rows have passed
(only that app knows that a broadcast must outlive its replies, or that a
webhook still waiting to be booked is never old). The function takes the
cutoff and ``dry_run`` and answers ``{label: rows}``.

What this does not delete, and why:

* The audit trail (``vs_audit.AuditEvent``, which also holds sign-in events).
  The database refuses every delete there by trigger; keeping it for 6 years
  needs a trigger change of its own.
* Record edit history (``vs_history.RecordVersion``). The "as at a date" views
  read the oldest version as the day history began, so pruning it needs a new
  baseline written first.
* Anything held by :mod:`core.retention` (the books): a held row refuses the
  delete and the entry is reported as stopped, never forced.

Rows go in batches, each in its own transaction, so a large first run never
holds one long lock, and a run stopped halfway resumes the next night.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

BATCH = 1000
SIX_YEARS = 6 * 365 + 2


@dataclass(frozen=True)
class Period:
    key: str
    days: int
    what: str
    prune: str  # dotted path to ``prune(cutoff, *, dry_run) -> {label: rows}``


SCHEDULE: tuple[Period, ...] = (
    Period(
        "sign_in", 365,
        "sign-in attempts, sessions, used or expired sign-in links and codes, "
        "and phones or browsers not seen for a year",
        "vs_user.pruning.prune_sign_in_records",
    ),
    Period(
        "refresh_tokens", 0,
        "refresh tokens past their expiry",
        "vs_user.pruning.prune_expired_refresh_tokens",
    ),
    Period(
        "notifications", 365,
        "delivered or failed notifications and emails",
        "vs_notifications.pruning.prune_notifications",
    ),
    Period(
        "payment_webhooks", 365,
        "payment provider webhooks already handled",
        "vs_payments.pruning.prune_handled_webhooks",
    ),
    Period(
        "messages", 3 * 365,
        "conversations with no message for 3 years",
        "schools.vs_messages.pruning.prune_quiet_conversations",
    ),
    Period(
        "support_tickets", 2 * 365,
        "support tickets closed 2 years ago",
        "vs_tickets.pruning.prune_closed_tickets",
    ),
    # Six years, leap days included, after a person leaves.
    Period(
        "former_pupils_and_parents", SIX_YEARS,
        "pupils who left 6 years ago, and parents whose last child has been erased",
        "schools.vs_students.erasure.erase_leavers",
    ),
    Period(
        "former_staff", SIX_YEARS,
        "school staff who left 6 years ago",
        "schools.vs_staff.erasure.erase_leavers",
    ),
    Period(
        "former_codex_staff", SIX_YEARS,
        "CodeX staff who exited 6 years ago",
        "vs_user.erasure.erase_platform_leavers",
    ),
)


def delete_in_batches(queryset, *, dry_run: bool = False) -> int:
    """Delete every row of ``queryset`` a batch at a time; answer how many.

    The queryset is re-read for each batch, so rows that pass their date
    mid-run are taken too, and a batch that fails leaves the ones before it
    deleted. Deleting through the ORM keeps every signal (a ticket's files are
    revoked, a held row refuses).
    """
    if dry_run:
        return queryset.count()
    model = queryset.model
    removed = 0
    while True:
        batch = list(queryset.values_list("pk", flat=True)[:BATCH])
        if not batch:
            return removed
        with transaction.atomic():
            model._base_manager.filter(pk__in=batch).delete()
        removed += len(batch)


def run(*, now: datetime.datetime | None = None, dry_run: bool = False) -> dict:
    """Prune every entry of :data:`SCHEDULE`; one failing never stops the rest."""
    now = now or timezone.now()
    report = {}
    for period in SCHEDULE:
        cutoff = now - datetime.timedelta(days=period.days)
        try:
            rows = import_string(period.prune)(cutoff, dry_run=dry_run)
            report[period.key] = {"cutoff": cutoff.isoformat(), "rows": rows}
        except Exception as exc:  # a held or protected row, or a fault: log, go on
            logger.exception("Pruning %s stopped", period.key)
            report[period.key] = {"cutoff": cutoff.isoformat(), "stopped": str(exc)}
    return {"dry_run": dry_run, "periods": report}
