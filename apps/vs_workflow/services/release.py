"""Continuing a submission that nobody can approve.

:mod:`vs_workflow.services.parking` explains the state this operates on. A stage whose
template sets ``skip_if_no_approvers=False`` activates with an empty approver snapshot
when nobody holds its role, and the document *parks*: ACTIVE stage, IN_PROGRESS
instance, no human able to decide it. Parking is the safe failure - money must never
approve itself - and the repair frees it as soon as somebody is appointed to the role.

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
      including somebody appointed to the role one second ago - the release is refused
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

from vs_workflow.constants import (
    ApproverSource,
    AuditEventType,
    OrganogramTarget,
    WorkflowInstanceStatus,
)
from vs_workflow.services import approvers as approvers_service
from vs_workflow.services import audit as audit_service
from vs_workflow.services import parking
from vs_workflow.services import routing as routing_service

#: Recorded in ``skip_reason`` on the stage the release stepped past, so a stage trail
#: distinguishes "no approver, and somebody chose to continue" from an ordinary skip.
RELEASE_SKIP_REASON = "Released at submission: no approver was available."

#: Used when the caller supplies none. A release is always explicable even when the
#: person clicking through a dialog is not asked to type anything.
DEFAULT_REASON = "Continued without approval: nobody held the approving role."

#: Same ceiling procurement's override uses, so one field's rules do not differ by module.
MAX_REASON_LENGTH = 500


class NotParkedError(Exception):
    """The instance has somebody who can decide it, so there is nothing to release."""


class ReleaseNotAllowedError(Exception):
    """The document handler forbids completing approval without a human vote."""


def handler_allows_release(instance) -> bool:
    """Whether this document type permits the generic unstaffed-stage release."""
    from vs_workflow.exceptions import UnknownDocumentTypeError
    from vs_workflow.handlers import get_handler

    try:
        handler = get_handler(instance.document_type)
    except UnknownDocumentTypeError:
        # Preserve the existing behavior for orphaned legacy document types. The
        # release still requires a genuinely parked stage and an authorized actor.
        return True
    return bool(getattr(handler, "allows_continue_without_approval", True))


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
    since been appointed to the role - correctly reports ``None`` and the caller offers
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


def stage_requirement(stage) -> str:
    """One plain sentence naming what would give this stage an approver.

    The dialog has to tell somebody how to fix the situation properly, and "no approver"
    does not. What fixes it depends entirely on how the stage resolves approvers, so the
    sentence is composed here, next to the model that knows.

    **Every source gets a usable sentence, including ones added after this was written.**
    An unrecognised source falls back to a truthful generic line rather than an empty
    string, because the fallback is what a client renders when the approver model
    changes underneath it. A vague sentence is recoverable; a blank space where the
    instruction should be is not.
    """
    source = stage.approver_source
    if source == ApproverSource.ROLE:
        role_key = approvers_service.stage_role_key(stage)
        if role_key:
            return f"assign someone to the {role_key} role"
    if source == ApproverSource.WORKFLOW_GROUP and stage.approver_group_id:
        return (f"add someone to the {stage.approver_group.name} approver group")
    if source == ApproverSource.DYNAMIC_ROLE:
        # Which rule fires depends on the document, so the sentence names the
        # rule set rather than guessing at one role.
        return ("assign someone to the role this step's rules select for this "
                "document")
    if source == ApproverSource.ORGANOGRAM:
        target = stage.organogram_target
        if target == OrganogramTarget.DIRECT_MANAGER:
            return "give the person who raised this a manager on the organogram"
        if target == OrganogramTarget.N_LEVELS_UP:
            levels = stage.organogram_levels or 1
            return (
                f"complete the reporting line above the person who raised this "
                f"({levels} level{'s' if levels != 1 else ''} up)"
            )
        if target == OrganogramTarget.DEPARTMENT_HEAD:
            return "appoint a head for the department of the person who raised this"
        if target == OrganogramTarget.SPECIFIC_POSITION:
            position = stage.organogram_position
            seat = getattr(position, "title", "") or getattr(position, "name", "")
            return f"put somebody in the {seat} position" if seat else (
                "put somebody in the position this step approves from")
        return "complete the organogram around the person who raised this"
    # Deliberately generic: a source this function has not been taught about is a
    # configuration question, and pointing at the template is always true.
    return "configure an approver for this step on the workflow template"


def describe_park(instance) -> dict:
    """What the submitter needs to be told, or ``{"parked": False}``.

    Shaped for a confirmation dialog: whether the document is stuck, which decision it
    is stuck on, and what would unstick it properly.

    The client is given **facts plus a ready-made sentence**, not just a role key.
    ``role_key`` is only meaningful for a role-sourced stage and is blank
    otherwise, so a client that wants to render it specially must check
    ``approver_source`` first; ``requirement`` is always populated and always safe to
    show. That split is what lets the approver model change without the dialog going
    blank or, worse, telling somebody to grant something that no longer decides
    anything.
    """
    stage_instance = parked_stage(instance)
    can_continue = handler_allows_release(instance)
    if stage_instance is None:
        return {"parked": False, "can_continue_without_approval": can_continue}
    stage = stage_instance.stage
    return {
        "parked": True,
        "stage_code": stage.code,
        "stage_label": stage.label,
        "approver_source": stage.approver_source,
        # Blank unless this stage really resolves by a named role; see the docstring.
        "role_key": (
            approvers_service.stage_role_key(stage)
            if stage.approver_source == ApproverSource.ROLE else ""
        ),
        "requirement": stage_requirement(stage),
        "document_type": instance.document_type,
        "can_continue_without_approval": can_continue,
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
    if not handler_allows_release(instance):
        raise ReleaseNotAllowedError(
            "This document type requires a human approval and cannot continue without one.",
        )
    reason_text = _clean_reason(reason)

    # A repair pass runs first: if anybody has since been granted the approving
    # role this returns None, and the answer is "get them to decide it".
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
            # The audit row must survive a change in how stages resolve approvers, so
            # it records the source and the requirement alongside the key rather than
            # relying on a key that may stop being the deciding factor.
            "approver_source": stage_instance.stage.approver_source,
            "role_key": approvers_service.stage_role_key(stage_instance.stage),
            "requirement": stage_requirement(stage_instance.stage),
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
