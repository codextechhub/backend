"""
Action recording - record_action, withdraw, cancel, reverse_action, resubmit.
Every function acquires select_for_update on the instance (F2 - pessimistic locking).

Each of these hands the outcome to the module that owns the document, through the
handler contract in ``vs_workflow.handlers.base``, because the engine's record of a
decision and the document's own state have to say the same thing. Reversal is the
only one that asks first as well as tells afterwards: it is the operation that
declares a recorded outcome untrue, and a decision already acted on outside the
engine cannot be undone by editing the engine's account of it.
"""

import logging

from django.db import transaction
from django.utils import timezone

from vs_workflow.constants import (
    AuditEventType, StageAdvanceRule, StageOnRejection,
    WorkflowInstanceStatus, WorkflowStageStatus,
    WorkflowStageAction as StageActionEnum,
)
from vs_workflow.exceptions import (
    CancellationNotAllowedError, DuplicateApproverActionError,
    InstanceTerminalError, InvalidInstanceStateError,
    NotAnEligibleApproverError, RequesterCannotApproveError,
    ReversalNotAllowedError, StageNotActiveError, UnknownDocumentTypeError,
)
from vs_workflow.handlers import get_handler
from vs_workflow.models import WorkflowInstance, WorkflowStageAction, WorkflowStageApprover, WorkflowStageInstance
from vs_workflow.services import approvers as approvers_service
from vs_workflow.services import audit as audit_service
from vs_workflow.services import routing as routing_service

logger = logging.getLogger(__name__)


# Run document-specific side effects after the workflow state change succeeds.
def _run_handler_callback(instance, method: str, context: dict) -> None:
    """Invoke a document handler lifecycle callback (on_cancelled, on_withdrawn,
    …) without letting a missing handler block the engine's own state change.

    A document_type with no registered handler (e.g. an in-flight instance left
    over after the type was renamed) must not stop an admin from cancelling or a
    requester from withdrawing. Genuine handler bugs still propagate.
    """
    try:
        handler = get_handler(instance.document_type)
    except UnknownDocumentTypeError:
        logger.warning(
            "No workflow handler for document_type '%s'; skipping %s on instance %s.",
            instance.document_type, method, instance.id,
        )
        return
    getattr(handler, method)(instance, context)


# Lock a workflow instance before mutating state.
def _lock_instance(instance_id) -> WorkflowInstance:
    """Fetch the instance under a row-level write lock (SELECT FOR UPDATE).

    Every mutating operation acquires this lock first so concurrent requests
    (two approvers voting at the same millisecond, a withdraw racing a cancel)
    serialize safely instead of producing duplicate state transitions or split
    advance_rule counts.
    """
    return WorkflowInstance.objects.select_for_update().get(pk=instance_id)


# Locate the active stage attempt that should receive the next action.
def _active_stage_instance(instance: WorkflowInstance) -> WorkflowStageInstance:
    """Return the single ACTIVE WorkflowStageInstance for the current stage.

    Ordering by attempt descending and taking .first() guards against the edge
    case where a resubmit creates a new attempt before the previous one is fully
    resolved - callers always operate on the latest attempt in progress.
    """
    if instance.current_stage is None:
        raise StageNotActiveError("No active stage on this instance.")
    si = (WorkflowStageInstance.objects
          .filter(instance=instance, stage=instance.current_stage,
                  status=WorkflowStageStatus.ACTIVE)
          .order_by("-attempt").first())
    if si is None:
        raise StageNotActiveError("No ACTIVE stage instance found.")
    return si


# Verify the actor belongs to the frozen approver snapshot.
def _check_eligibility(stage_instance, actor) -> WorkflowStageApprover:
    """Verify the actor is in the frozen approver snapshot for this stage attempt.

    Eligibility is determined at stage activation time and stored in
    WorkflowStageApprover. Checking the snapshot (not re-resolving live RBAC)
    means a role change mid-workflow doesn't retroactively invalidate
    an approver who was already notified and is mid-review.
    """
    snap = WorkflowStageApprover.objects.filter(
        stage_instance=stage_instance, attempt=stage_instance.attempt, user=actor).first()
    if snap is None:
        raise NotAnEligibleApproverError("User is not on the eligible approver list.")
    return snap


# Determine whether the current stage has enough approvals to advance.
def _stage_fully_approved(stage_instance: WorkflowStageInstance) -> bool:
    """Check whether the stage's advance_rule threshold has been met.

    Counts only non-reversed, non-reversal APPROVED actions on the current
    attempt. Reversed votes are excluded so an admin reversal correctly
    re-opens a stage that had already crossed the threshold.
    """
    stage = stage_instance.stage
    approved_count = WorkflowStageAction.objects.filter(
        stage_instance=stage_instance, attempt=stage_instance.attempt,
        action=StageActionEnum.APPROVED, reversed_at__isnull=True,
        is_reversal_of__isnull=True).count()
    eligible_count = WorkflowStageApprover.objects.filter(
        stage_instance=stage_instance, attempt=stage_instance.attempt).count()
    rule = StageAdvanceRule(stage.advance_rule)
    if rule == StageAdvanceRule.ANY:
        return approved_count >= 1
    if rule == StageAdvanceRule.QUORUM:
        return approved_count >= (stage.quorum_count or 1)
    return eligible_count > 0 and approved_count >= eligible_count


# Record one approver decision and advance or terminate the workflow as needed.
def record_action(instance_id, actor, action: str, comment: str = "") -> WorkflowInstance:
    """Record an approver vote: APPROVED, REJECTED, or RETURNED."""
    if action not in {StageActionEnum.APPROVED, StageActionEnum.REJECTED, StageActionEnum.RETURNED}:
        raise InvalidInstanceStateError(f"Unsupported action '{action}'.")
    if action in {StageActionEnum.REJECTED, StageActionEnum.RETURNED} and not comment.strip():
        raise InvalidInstanceStateError("Comment required for REJECTED and RETURNED actions.")

    with transaction.atomic():
        instance = _lock_instance(instance_id)
        if instance.is_terminal:
            raise InstanceTerminalError(instance=str(instance.id), status=instance.status)
        if instance.status == WorkflowInstanceStatus.RETURNED:
            raise InvalidInstanceStateError("Instance is RETURNED. Wait for resubmission.")
        if actor.pk == instance.requested_by_id:
            raise RequesterCannotApproveError("Requesters cannot approve their own documents.")

        si = _active_stage_instance(instance)
        snap = _check_eligibility(si, actor)

        # One active vote per actor per attempt keeps quorum math stable.
        if WorkflowStageAction.objects.filter(
            stage_instance=si, actor=actor, attempt=si.attempt,
            is_reversal_of__isnull=True, reversed_at__isnull=True).exists():
            raise DuplicateApproverActionError("You have already voted on this stage.")

        action_row = WorkflowStageAction.objects.create(
            stage_instance=si, actor=actor, on_behalf_of=snap.on_behalf_of,
            action=action, comment=comment, attempt=si.attempt,
        )
        audit_service.write(instance, AuditEventType.APPROVER_ACTED, actor=actor,
                            stage_instance=si,
                            context={"action": action, "comment": comment,
                                     "attempt": si.attempt, "action_id": str(action_row.id)})

        if action == StageActionEnum.RETURNED:
            # RETURNED pauses the workflow and hands control back to the requester.
            si.status = WorkflowStageStatus.RETURNED
            si.resolved_at = timezone.now()
            si.save(update_fields=["status", "resolved_at"])
            return routing_service._return_to_requester(instance, actor, comment, si.stage_id)

        if action == StageActionEnum.REJECTED:
            # Rejection either terminates the workflow or returns it based on stage policy.
            si.status = WorkflowStageStatus.REJECTED
            si.resolved_at = timezone.now()
            si.save(update_fields=["status", "resolved_at"])
            audit_service.write(instance, AuditEventType.STAGE_REJECTED, actor=actor,
                                stage_instance=si)
            if si.stage.on_rejection == StageOnRejection.TERMINAL:
                return routing_service._terminate_rejected(instance, actor, comment)
            return routing_service._return_to_requester(instance, actor, comment, si.stage_id)

        # Approval advances only after the stage's configured threshold is met.
        if _stage_fully_approved(si):
            si.status = WorkflowStageStatus.APPROVED
            si.resolved_at = timezone.now()
            si.save(update_fields=["status", "resolved_at"])
            audit_service.write(instance, AuditEventType.STAGE_APPROVED, stage_instance=si)
            routing_service.advance_instance(instance, current_attempt=si.attempt)
        return instance


# Let the requester stop a non-terminal workflow they submitted.
def withdraw(instance_id, requester) -> WorkflowInstance:
    """Requester withdraws their submission. Always permitted until final approval."""
    with transaction.atomic():
        instance = _lock_instance(instance_id)
        if instance.is_terminal:
            raise InstanceTerminalError(instance=str(instance.id), status=instance.status)
        if requester.pk != instance.requested_by_id:
            raise InvalidInstanceStateError("Only the requester can withdraw.")
        # Withdrawal is terminal and clears the active stage pointer.
        instance.status = WorkflowInstanceStatus.WITHDRAWN
        instance.current_stage = None
        instance.completed_at = timezone.now()
        instance.state_version += 1
        instance.save(update_fields=["status", "current_stage", "completed_at",
                                      "state_version", "updated_at"])
        audit_service.write(instance, AuditEventType.INSTANCE_WITHDRAWN, actor=requester)
        _run_handler_callback(instance, "on_withdrawn", {"actor_id": str(requester.pk)})
        return instance


# Let an admin terminally cancel a stuck or invalid workflow.
def cancel(instance_id, admin, reason: str) -> WorkflowInstance:
    """Admin cancels a stuck instance. Terminal; requester starts over."""
    if not reason.strip():
        raise CancellationNotAllowedError("Cancellation reason is required.")
    with transaction.atomic():
        instance = _lock_instance(instance_id)
        if instance.is_terminal:
            raise InstanceTerminalError(instance=str(instance.id), status=instance.status)
        # Cancellation is terminal; a requester must submit a fresh instance.
        instance.status = WorkflowInstanceStatus.CANCELLED
        instance.current_stage = None
        instance.completed_at = timezone.now()
        instance.state_version += 1
        instance.save(update_fields=["status", "current_stage", "completed_at",
                                      "state_version", "updated_at"])
        audit_service.write(instance, AuditEventType.INSTANCE_CANCELLED,
                            actor=admin, context={"reason": reason})
        _run_handler_callback(instance, "on_cancelled",
                              {"actor_id": str(admin.pk), "reason": reason})
        return instance


# Resolve the handler that has to agree before a decision is undone.
def _reversal_handler(instance):
    """Return the document handler, refusing the reversal when there is none.

    Cancellation and withdrawal tolerate a missing handler because they are the
    escape hatches that must work on a stuck instance. Reversal is the opposite
    case: it declares a recorded outcome untrue, and the only party that knows
    whether that outcome has already been acted on is the module that owns the
    document. With nobody to ask, the answer is no.
    """
    try:
        return get_handler(instance.document_type)
    except UnknownDocumentTypeError as exc:
        raise ReversalNotAllowedError(
            f"No handler is registered for document type "
            f"'{instance.document_type}', so nothing can confirm this decision "
            f"is safe to undo.",
            document_type=instance.document_type,
        ) from exc


# Void one action row and append the reversal that records it.
def _void_action(action: WorkflowStageAction, admin, reason: str,
                 comment: str) -> WorkflowStageAction:
    """Reverse a single recorded vote, append-only.

    The original keeps its own text and gains reversed_at/reversed_by; a new row
    records the reversal against the admin who ordered it. Nothing is deleted,
    so the trail still answers who voted what and who undid it.
    """
    action.reversed_at = timezone.now()
    action.reversed_by = admin
    action.reversal_reason = reason
    action.save(update_fields=["reversed_at", "reversed_by", "reversal_reason"])
    return WorkflowStageAction.objects.create(
        stage_instance=action.stage_instance, actor=admin,
        action=action.action, comment=comment,
        attempt=action.attempt, is_reversal_of=action,
    )


# Decide whether the reversed vote is what resolved its own stage.
def _vote_resolved_its_stage(stage_instance: WorkflowStageInstance,
                             original: WorkflowStageAction) -> bool:
    """Report whether reopening the stage is the honest consequence of the reversal.

    A rejection or a return resolves a stage by itself, so undoing it always
    reopens the stage. An approval only resolved the stage if the stage no longer
    meets its threshold without it: reversing one approval of two on an ANY stage,
    or one of three where two still make quorum, takes away a vote that was never
    load-bearing, and reopening a stage that is still satisfied would knock the
    workflow backwards for nothing.

    Called after the vote has been voided, so the threshold count already excludes it.
    """
    if original.action == StageActionEnum.APPROVED:
        return (stage_instance.status == WorkflowStageStatus.APPROVED
                and not _stage_fully_approved(stage_instance))
    if original.action == StageActionEnum.REJECTED:
        return stage_instance.status == WorkflowStageStatus.REJECTED
    return stage_instance.status == WorkflowStageStatus.RETURNED


# Roll back every stage that only ran because the reopened one had completed.
def _undo_downstream_stages(instance: WorkflowInstance,
                            from_stage_instance: WorkflowStageInstance,
                            admin, reason: str) -> list:
    """Return the codes of the later stages this reversal rolls back to PENDING.

    A later stage exists only because an earlier one completed. Leaving those
    rows APPROVED while their reason for running is withdrawn produces a workflow
    nobody can finish: re-approving the reopened stage walks straight back into a
    stage that already holds a live vote, its approver is refused as a duplicate,
    and the instance sits ACTIVE with no one able to move it.

    So each of them returns to PENDING with its votes voided and its approver
    snapshot cleared, and re-approval activates it as a genuinely fresh run that
    resolves eligibility again. The audit log keeps every activation and vote that
    happened, and each voided row keeps the reason it was undone.

    The engine runs one stage at a time, so "later" is "activated after this one".
    """
    if from_stage_instance.activated_at is None:
        return []
    undone = []
    downstream = (WorkflowStageInstance.objects.select_for_update()
                  .filter(instance=instance,
                          activated_at__gt=from_stage_instance.activated_at)
                  .exclude(pk=from_stage_instance.pk)
                  .order_by("activated_at"))
    for si in downstream:
        live_votes = WorkflowStageAction.objects.select_for_update().filter(
            stage_instance=si, attempt=si.attempt,
            is_reversal_of__isnull=True, reversed_at__isnull=True)
        for vote in live_votes:
            _void_action(
                vote, admin, reason,
                comment=(f"Undone with the reversal on stage "
                         f"{from_stage_instance.stage.code}: {reason}"),
            )
        WorkflowStageApprover.objects.filter(stage_instance=si, attempt=si.attempt).delete()
        si.status = WorkflowStageStatus.PENDING
        si.activated_at = None
        si.resolved_at = None
        si.skip_reason = ""
        si.save(update_fields=["status", "activated_at", "resolved_at", "skip_reason"])
        undone.append(si.stage.code)
    return undone


# Reverse a prior approver action and unwind whatever that action decided.
def reverse_action(action_id, admin, reason: str) -> WorkflowStageAction:
    """Admin reverses a recorded approver vote and unwinds what it decided.

    Three things happen, in this order, inside one transaction:

    1. the module that owns the document is asked whether the decision can still
       be undone (``validate_reversal``). It runs before any write, so a refusal
       leaves the approval intact;
    2. the vote is voided; if it is what resolved its stage, that stage reopens
       and every stage that ran afterwards is rolled back to PENDING with its own
       votes voided;
    3. the same module is told to put the document back (``on_action_reversed``),
       inside this transaction, so a handler that cannot comply rolls the whole
       reversal back rather than leaving the two descriptions disagreeing.

    A vote that did not resolve its stage is voided and nothing else moves: no
    stage reopens, and the document is not asked to change either. The owning
    module is still consulted first, because whether the decision may be touched
    at all does not depend on how many votes were behind it.
    """
    if not reason.strip():
        raise ReversalNotAllowedError("Reversal reason is required.")
    with transaction.atomic():
        original = WorkflowStageAction.objects.select_for_update().get(pk=action_id)
        if original.is_reversal_of_id is not None:
            raise ReversalNotAllowedError("Cannot reverse a reversal row.")
        if original.reversed_at is not None:
            raise ReversalNotAllowedError("Action is already reversed.")

        instance = _lock_instance(original.stage_instance.instance_id)
        if instance.is_terminal and instance.status != WorkflowInstanceStatus.APPROVED:
            raise ReversalNotAllowedError(
                f"Cannot reverse on instance in status {instance.status}.")

        si = WorkflowStageInstance.objects.select_for_update().get(
            pk=original.stage_instance_id)
        # A vote from a superseded attempt describes a run the workflow has
        # already left. Reopening it would put the instance back on a stage the
        # requester has since resubmitted past.
        if WorkflowStageInstance.objects.filter(
                instance=instance, stage=si.stage, attempt__gt=si.attempt).exists():
            raise ReversalNotAllowedError(
                "This stage has run again since that vote, so reversing it would "
                "not describe where the workflow now stands.",
                stage=si.stage.code, attempt=si.attempt)

        handler = _reversal_handler(instance)
        context = {
            "action_id": str(original.id),
            "original_action": original.action,
            "stage_code": si.stage.code,
            "attempt": original.attempt,
            "reason": reason,
            "actor_id": str(admin.pk),
            "was_final_approval": instance.status == WorkflowInstanceStatus.APPROVED,
        }
        # Ask before writing: the engine can undo its record of a decision, never
        # the decision's effect in the world.
        handler.validate_reversal(instance, context)

        reversal = _void_action(original, admin, reason,
                                comment=f"Admin reversal: {reason}")

        unwound = []
        reopened = None
        if _vote_resolved_its_stage(si, original):
            unwound = _undo_downstream_stages(instance, si, admin, reason)
            si.status = WorkflowStageStatus.ACTIVE
            si.resolved_at = None
            si.save(update_fields=["status", "resolved_at"])
            reopened = si.stage
            instance.current_stage = si.stage
            if instance.status in {WorkflowInstanceStatus.APPROVED,
                                   WorkflowInstanceStatus.RETURNED}:
                # A decided instance goes back into active review at this stage.
                instance.status = WorkflowInstanceStatus.IN_PROGRESS
                instance.completed_at = None
            instance.state_version += 1
            instance.save(update_fields=["status", "current_stage", "completed_at",
                                          "state_version", "updated_at"])

        audit_service.write(instance, AuditEventType.ACTION_REVERSED, actor=admin,
                            stage_instance=si,
                            context={"reversed_action_id": str(original.id),
                                     "original_action": original.action,
                                     "reason": reason,
                                     "reopened_stage": reopened.code if reopened else None,
                                     "unwound_stages": unwound})

        if reopened is not None:
            # Only a reversal that changed the workflow's outcome is one the
            # document has to follow. A vote the stage did not need is voided
            # and the stage still stands, so telling the owning module to put
            # its document back would move a document nothing has moved.
            context["reopened_stage_code"] = reopened.code
            context["unwound_stages"] = unwound
            handler.on_action_reversed(instance, context)
        return reversal


# Resume a returned workflow from the returning stage with a fresh attempt.
def resubmit(instance_id, requester) -> WorkflowInstance:
    """Requester resubmits after RETURNED. Resumes from returning stage, new attempt."""
    with transaction.atomic():
        instance = _lock_instance(instance_id)
        if instance.status != WorkflowInstanceStatus.RETURNED:
            raise InvalidInstanceStateError("resubmit requires RETURNED status.")
        if requester.pk != instance.requested_by_id:
            raise InvalidInstanceStateError("Only the requester can resubmit.")

        returning_stage = instance.current_stage
        if returning_stage is None:
            raise InvalidInstanceStateError("Returned instance has no stage to resume.")

        latest = (WorkflowStageInstance.objects
                  .filter(instance=instance, stage=returning_stage)
                  .order_by("-attempt").first())
        next_attempt = (latest.attempt + 1) if latest else 1

        audit_service.write(instance, AuditEventType.INSTANCE_RESUBMITTED, actor=requester,
                            context={"resuming_stage": returning_stage.code,
                                     "attempt": next_attempt})

        # If the stage was retired from the template while this instance was
        # sitting in RETURNED, don't re-activate it - advance past it instead.
        if returning_stage.retired_at is not None:
            instance.status = WorkflowInstanceStatus.IN_PROGRESS
            instance.state_version += 1
            instance.save(update_fields=["status", "state_version", "updated_at"])
            return routing_service.advance_instance(instance, current_attempt=next_attempt)

        # Re-activate stage with fresh approver snapshot.
        routing_service._activate_stage(instance, returning_stage, next_attempt)
        instance.status = WorkflowInstanceStatus.IN_PROGRESS
        instance.state_version += 1
        instance.save(update_fields=["status", "state_version", "updated_at"])

        eligible = approvers_service.resolve_approvers(returning_stage, instance)
        if not eligible and returning_stage.skip_if_no_approvers:
            # A returned stage can still auto-skip if no approvers are eligible anymore.
            routing_service._skip_stage(instance, returning_stage, next_attempt,
                                         AuditEventType.STAGE_SKIPPED_NO_APPROVER,
                                         "zero_eligible_on_resubmit")
            routing_service.advance_instance(instance, current_attempt=next_attempt)
        return instance
