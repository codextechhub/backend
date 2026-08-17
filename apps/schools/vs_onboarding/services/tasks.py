"""FR-003: moving one onboarding task, and everything that follows from it.

A transition is not just a column write. It re-decides readiness, it writes the
trail, and when a step completes it tells the people watching. All of that is
one transaction plus one after-commit dispatch, so a school can never see a
task go green while the gate still thinks it is red.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType

from ..constants import (
    ALLOWED_TASK_TRANSITIONS,
    EVENT_STEP_COMPLETED,
    ReadinessState,
    TaskStatus,
)
from ..exceptions import (
    InvalidTaskTransition,
    OnboardingAlreadyLive,
    OnboardingNotProvisioned,
    RequiredTaskNotSkippable,
    TaskAlreadyInState,
    TaskConditionNotMet,
)
from ..models import OnboardingProgress, OnboardingTask
from . import effects
from .conditions import condition_holds, condition_reason
from .readiness import ReadinessService
from .scoping import rows_for


#: Which action type records which move. A task starting work from
#: NOT_STARTED is an ordinary update: the closed onboarding vocabulary names
#: completion, skipping and reopening, and inventing a fourth value would mean
#: an unregistered action type, which ``emit_audit_event`` swallows in silence.
def _action_type_for(previous: str, target: str) -> str:
    if target == TaskStatus.DONE:
        return AuditActionType.ONBOARDING_TASK_COMPLETED
    if target == TaskStatus.SKIPPED:
        return AuditActionType.ONBOARDING_TASK_SKIPPED
    if previous in (TaskStatus.DONE, TaskStatus.SKIPPED):
        return AuditActionType.ONBOARDING_TASK_REOPENED
    return AuditActionType.UPDATE


def _step_position(tenant, task) -> tuple[int, int]:
    """This task's place in the school's own checklist, 1-based.

    Counted over the tenant's rows rather than over the catalog. A school that
    was provisioned under an older catalog does not hold the same steps as one
    provisioned today, and being told it is "step 3 of 8" while looking at seven
    is the kind of small wrongness that makes a control room feel broken.
    """
    keys = list(
        rows_for(OnboardingTask, tenant)
        .order_by("order_index")
        .values_list("key", flat=True)
    )
    total = len(keys)
    number = keys.index(task.key) + 1 if task.key in keys else task.order_index
    return number, total


def transition_task(tenant, key: str, target: str, *, actor=None) -> OnboardingTask:
    """Move the task identified by ``key`` to ``target`` for ``tenant``.

    Raises the domain refusal that matches the reason, never a generic error:
    the client renders a different thing for "you cannot do that from here"
    (422), "it is already like that" (409), "the platform can see it is not
    actually done" (422) and "this step is required, so it cannot be skipped"
    (422).
    """
    with transaction.atomic():
        progress = (
            OnboardingProgress.all_objects
            .select_for_update()
            .filter(tenant=tenant)
            .first()
        )
        if progress is None:
            raise OnboardingNotProvisioned()
        if progress.readiness_state == ReadinessState.LIVE:
            # Whether a live school may be sent back through onboarding is an
            # open product question; until it is answered, live is closed.
            raise OnboardingAlreadyLive()

        task = (
            rows_for(OnboardingTask, tenant)
            .select_for_update()
            .filter(key=key)
            .first()
        )
        if task is None:
            # Not a domain refusal: a key this tenant does not have is a
            # missing row, and it answers the same 404 as another tenant's row
            # so the two are indistinguishable.
            from rest_framework.exceptions import NotFound

            raise NotFound("No onboarding task matches this key.")

        previous = task.status
        if previous == target:
            raise TaskAlreadyInState(state=target)
        if target not in ALLOWED_TASK_TRANSITIONS.get(previous, frozenset()):
            raise InvalidTaskTransition(from_state=previous, to_state=target)

        if target == TaskStatus.SKIPPED and task.is_required:
            # Checked after the edge, not before it: a DONE task asked to skip
            # is refused for travelling an edge that does not exist, and that
            # is true of an optional task too. This refusal is about the task.
            raise RequiredTaskNotSkippable(key=task.key)

        if target == TaskStatus.DONE:
            school = effects.school_of(tenant)
            if not condition_holds(task.key, tenant, school):
                raise TaskConditionNotMet(
                    key=task.key, reason=condition_reason(task.key),
                )

        task.status = target
        task.completed_at = timezone.now() if target == TaskStatus.DONE else None
        task.save(update_fields=["status", "completed_at", "updated_at"])

        # Readiness is recomputed from the rows as they now stand, by the one
        # authority, inside the same transaction as the write that changed them.
        tasks = list(rows_for(OnboardingTask, tenant))
        ReadinessService.evaluate(tenant, actor=actor, tasks=tasks)

        context = None
        event_key = None
        recipients = None
        if target == TaskStatus.DONE:
            number, total = _step_position(tenant, task)
            context = effects.school_context(tenant)
            context.update({
                "step_number": number,
                "total_steps": total,
                "step_name": task.title,
                "completed_by_name": effects.actor_name(actor),
            })
            event_key = EVENT_STEP_COMPLETED
            recipients = effects.notification_recipients(tenant)

        effects.record(
            tenant=tenant,
            action_type=_action_type_for(previous, target),
            entity_type="OnboardingTask",
            entity_id=task.pk,
            actor=actor,
            entity_label=task.title,
            before_data={"status": previous},
            diff_data={"status": {"from": previous, "to": target}},
            event_key=event_key,
            context=context,
            recipients=recipients,
        )

    return task
