"""FR-004: the single authority on whether a school may go live.

There is one method, and every caller uses it: the control room display, the
revalidate endpoint, every task transition and the go-live gate itself. That is
the whole point. A view that computed eligibility for itself would eventually
disagree with the gate, and the school would be told it is ready by a screen
that the server then refuses.

Readiness is decided by required task completion and by nothing else. Optional
tasks never block in any status; a required task blocks until it is DONE, and
it cannot be skipped past (``services.tasks`` refuses that outright), so the
only way out of NOT_READY is to finish the work. Neither LIVE nor
PENDING_APPROVAL is ever downgraded from here: a school waiting on a decision
is not sent back to the start because somebody reopened a step, it waits for
the decision.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..constants import EVENT_GO_LIVE_READY, ReadinessState, TaskStatus
from ..models import OnboardingProgress, OnboardingTask
from . import effects
from .scoping import rows_for


def outstanding_required_keys(tenant) -> list[str]:
    """The required task keys that are not DONE, in display order."""
    return list(
        rows_for(OnboardingTask, tenant)
        .filter(is_required=True)
        .exclude(status=TaskStatus.DONE)
        .order_by("order_index")
        .values_list("key", flat=True)
    )


class ReadinessService:
    """The one place ``readiness_state`` is computed and written."""

    #: States that describe a decision already in flight or already taken.
    #: Neither is recomputed downwards: a rejection or a withdrawal moves a
    #: tenant out of PENDING_APPROVAL, and nothing moves it out of LIVE.
    TERMINAL_STATES = frozenset({
        ReadinessState.PENDING_APPROVAL, ReadinessState.LIVE,
    })

    @staticmethod
    def evaluate(tenant, *, actor=None, tasks=None) -> OnboardingProgress | None:
        """Recompute and stamp readiness for ``tenant``.

        Returns the progress row, or None when the tenant has none (an
        unprovisioned tenant has no readiness to recompute, and creating one
        here would hide the fact that it was never set up).

        ``last_validation_at`` is stamped on every evaluation, including one
        that changes nothing: "we checked and it is still not ready" is the
        answer the control room needs to show a timestamp for.

        ``tasks`` lets a caller that has already loaded the task rows pass them
        in, so a transition does not re-read what it just wrote.
        """
        progress = OnboardingProgress.all_objects.filter(tenant=tenant).first()
        if progress is None:
            return None

        previous = progress.readiness_state
        now = timezone.now()
        progress.last_validation_at = now

        if previous in ReadinessService.TERMINAL_STATES:
            progress.save(update_fields=["last_validation_at", "updated_at"])
            return progress

        if tasks is None:
            tasks = list(rows_for(OnboardingTask, tenant))
        blocking = [
            task.key for task in tasks
            if task.is_required and task.status != TaskStatus.DONE
        ]
        target = ReadinessState.NOT_READY if blocking else ReadinessState.READY

        progress.readiness_state = target
        progress.save(update_fields=[
            "readiness_state", "last_validation_at", "updated_at",
        ])

        if previous != ReadinessState.READY and target == ReadinessState.READY:
            ReadinessService._announce_ready(tenant, progress, actor)
        return progress

    @staticmethod
    def _announce_ready(tenant, progress, actor):
        """Tell the audience the school has cleared the gate.

        Emitted every time the tenant enters READY, including re-entry after a
        rejection. There is deliberately no suppression window: the second time
        a school becomes ready is exactly as newsworthy as the first, and more
        so, because somebody is waiting on it.
        """
        context = effects.school_context(tenant)
        context["completed_by_name"] = effects.actor_name(actor)

        def _notify():
            effects._send(
                EVENT_GO_LIVE_READY,
                context,
                effects.notification_recipients(tenant),
                tenant,
            )

        transaction.on_commit(_notify)
