"""Continuing a submission that nobody can approve.

:mod:`vs_workflow.services.parking` explains the state this operates on. A stage whose
template sets ``skip_if_no_approvers=False`` activates with an empty approver snapshot
when nobody holds its permission, and the document *parks*: ACTIVE stage, IN_PROGRESS
instance, no human able to decide it. Parking is the safe failure - money must never
approve itself - and the repair frees it as soon as somebody is granted the permission.

This module is the other exit: the submitter is told at submission that nobody can
approve this, and chooses to continue anyway.

The control this trades away, stated plainly
    Releasing a stage without a vote means the document reaches its terminal state with
    no second pair of eyes. On a payout batch or a purchase order that is the whole
    maker-checker control, and the only thing standing in its place afterwards is the
    record this module writes. That is a deliberate product decision (an unstaffed
    workflow that silently swallows work is worse than one a person can consciously step
    past), not an oversight, and it is why every release is named, timed, reasoned and
    written to an append-only log before the workflow is allowed to move.

    :mod:`vs_procurement.approval_override` is the stricter sibling: same mechanics, but
    behind a CRITICAL permission and a mandatory typed justification. It is unchanged.

What a release still may not do
    * **Bypass a human who exists.** The stage must be genuinely parked, re-checked after
      a repair pass and again under a row lock. If anybody at all can decide the stage -
      including somebody granted the permission one second ago - the release is refused
      and the answer is "get them to decide it". This is the property that keeps the
      dialog from being a self-approval button on a document that has a reviewer.
    * **Release more than one stage.** A ladder's later stages are separate decisions. If
      the next stage has approvers the document goes back into ordinary review; if it
      parks too, it needs its own release and its own record.
    * **Reach past its own tenant.** The caller resolves the instance under the ordinary
      scoping; nothing here widens it.

How the workflow terminates, and why that is safe
    The stage is resolved to SKIPPED through the engine's own ``routing._skip_stage``,
    then control is handed to the public ``routing.advance_instance``, which activates
    the next stage or terminates the instance APPROVED through the same
    ``_terminate_approved`` to ``handler.on_approved`` path a real final vote takes. The
    document therefore transitions exactly as an approved one does, which is what makes
    a released payout dispatch and a released purchase order issue. Hand-rolling that
    would be the more dangerous choice.
"""
from __future__ import annotations

from django.db import transaction

from vs_workflow.constants import AuditEventType, WorkflowInstanceStatus
from vs_workflow.services import audit as audit_service
from vs_workflow.services import parking
from vs_workflow.services import routing as routing_service

#: Recorded in ``skip_reason`` on the stage the release stepped past, so a stage trail
#: distinguishes "no approver, and somebody chose to continue" from an ordinary skip.
RELEASE_SKIP_REASON = "Released at submission: no approver was available."

#: Used when the caller supplies none. A release is always explicable even when the
#: person clicking through a dialog is not asked to type anything.
DEFAULT_REASON = "Continued without approval: nobody held the approving permission."

#: Same ceiling procurement's override uses, so one field's rules do not differ by module.
MAX_REASON_LENGTH = 500


class NotParkedError(Exception):
    """The instance has somebody who can decide it, so there is nothing to release."""


def _actor_label(user) -> str:
    """A display name for the audit message, never an email address."""
    return (getattr(user, "full_name", "") or "").strip() or user.get_full_name() or "A user"


def _clean_reason(reason) -> str:
    """Normalise the caller's justification without rewriting it.

    Only surrounding whitespace is stripped: the text is the actor's own words on an
    append-only record, so it is never summarised or truncated silently.
    """
    if reason is not None and not isinstance(reason, str):
        raise ValueError("The reason must be text.")
    text = (reason or "").strip() or DEFAULT_REASON
    if len(text) > MAX_REASON_LENGTH:
        raise ValueError(f"The reason cannot be longer than {MAX_REASON_LENGTH} characters.")
    return text


def parked_stage(instance):
    """The ACTIVE, unstaffed stage holding ``instance`` up, or ``None``.

    Runs a repair pass first, so an instance that only *looked* parked - somebody has
    since been granted the permission - correctly reports ``None`` and the caller offers
    review rather than a bypass. Callers that intend to act must still re-assert the
    precondition under a row lock; :func:`release_parked_stage` does.
    """
    if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
        return None
    parking.repair_workflows(instance_id=instance.id)
    return (
        parking.empty_active_stages()
        .filter(instance_id=instance.id)
        .order_by("-attempt")
        .first()
    )


def describe_park(instance) -> dict:
    """What the submitter needs to be told, or ``{"parked": False}``.

    Shaped for a confirmation dialog: whether the document is stuck, which decision it
    is stuck on, and which permission nobody holds, so the warning can name the thing an
    administrator would have to grant instead of saying "no approver" and leaving them
    to guess.
    """
    stage_instance = parked_stage(instance)
    if stage_instance is None:
        return {"parked": False}
    stage = stage_instance.stage
    return {
        "parked": True,
        "stage_code": stage.code,
        "stage_label": stage.label,
        "permission_key": stage.approver_permission_key or "",
        "document_type": instance.document_type,
    }


@transaction.atomic
def release_parked_stage(instance, *, actor_user, reason=None):
    """Step past ``instance``'s parked stage without a vote, and record who did.

    Returns the released :class:`~vs_workflow.models.WorkflowStageInstance`. Raises
    :class:`NotParkedError` when the stage can still be decided by a human, and
    ``ValueError`` on an unusable reason. Every refusal leaves nothing written: the whole
    release is one transaction, so a failure in the engine's terminal callbacks rolls the
    audit row back with it rather than claiming a release that did not happen.
    """
    reason_text = _clean_reason(reason)

    # A repair pass runs first: if anybody has since been granted the approving
    # permission this returns None, and the answer is "get them to decide it".
    stage_instance = parked_stage(instance)
    if stage_instance is not None:
        # Re-assert the same precondition under a row lock. Between the read above and
        # here a repair could have staffed the stage or a vote could have landed, and a
        # release applied on top of either would be the bypass this refuses.
        stage_instance = parking.lock_parked_stage(stage_instance.pk)
    if stage_instance is None:
        raise NotParkedError(
            "This is not waiting on an unstaffed approval stage, so it cannot be "
            "continued without approval.",
        )

    # Written before the workflow moves: if the engine's terminal callbacks run (a payout
    # dispatches, a purchase order issues), the evidence of why already exists.
    audit_service.write(
        instance, AuditEventType.APPROVER_ACTED, actor=actor_user,
        stage_instance=stage_instance,
        context={
            "action": "RELEASED_NO_APPROVER",
            "override": True,
            "stage_code": stage_instance.stage.code,
            "attempt": stage_instance.attempt,
            "permission_key": stage_instance.stage.approver_permission_key or "",
            "reason": reason_text,
        },
        message=(
            f"{_actor_label(actor_user)} continued without approval: no approver was "
            f"available."
        ),
    )
    # Resolve the stage the way the engine resolves one nobody ran, carrying a reason
    # that says which of the two it was.
    routing_service._skip_stage(
        instance, stage_instance.stage, stage_instance.attempt,
        AuditEventType.STAGE_SKIPPED_NO_APPROVER, RELEASE_SKIP_REASON,
    )
    # The engine's own public advance: it evaluates the remaining stages and either
    # activates the next one or terminates the instance APPROVED through the same
    # path a final vote would take.
    routing_service.advance_instance(instance, current_attempt=stage_instance.attempt)
    return stage_instance


def approval_block(instance) -> dict:
    """The submission-response payload a confirmation dialog needs.

    Every module's "submit for approval" endpoint returns this alongside its own
    document, so the client learns in the submit response itself that nobody can
    approve what it just submitted, and can offer to continue without a second round
    trip. Keeping the shape here rather than in each module's serializer is what stops
    four submit screens from growing four different answers to the same question.
    """
    return {"instance_id": str(instance.id), **describe_park(instance)}


def may_release(instance, user) -> bool:
    """Whether ``user`` may continue this submission without approval.

    The submitter, or platform staff cleaning up on a tenant's behalf. Deliberately not
    "any authenticated user in the tenant": the release is ungated by design as a
    *product* decision about the person who raised the document, and letting an
    unrelated user release somebody else's parked spend would be a different thing
    entirely, and a real hole.
    """
    from vs_rbac.permissions import is_vision_super_admin

    if getattr(user, "is_superuser", False) or is_vision_super_admin(user):
        return True
    return instance.requested_by_id == getattr(user, "pk", None)
