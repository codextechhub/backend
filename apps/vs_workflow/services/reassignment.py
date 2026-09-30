"""Changing who approves a request while it waits.

Two things change who approves a request that is already under way. An
administrator edits the list for one stage of one request, or replaces one
person with another across many requests at once. A delegation reaches the
requests already waiting on its delegator when it starts, and leaves them
again when it is revoked.

Two lists can change, and they are kept apart on purpose:

* **A stage that is waiting** is decided by its approver snapshot
  (:class:`~vs_workflow.models.WorkflowStageApprover`) for the current attempt.
  Every reader of approval authority in the platform reads those rows (the
  eligibility check, the approval count, the personal queue, and the modules
  that show who is deciding their documents), so a change is made to the rows
  themselves and those readers need no change of their own.
* **A stage that has not opened** has no rows yet. The administrator's choice
  is kept as a :class:`~vs_workflow.models.WorkflowStageAssignment`, which
  :func:`vs_workflow.services.approvers.resolve_approvers` reads in place of
  the stage's own source. Activation, re-activation after a return and the
  parking repair all resolve through that function, so each of them honours it.

Nothing is deleted from history. Every person added or taken off is written to
:class:`~vs_workflow.models.WorkflowApproverChange`, and every change also writes
a row to the request's audit log. A person put on a waiting stage is told exactly
as if the stage had just opened for them, and a person taken off is told the
request no longer needs them.

The rules the screens rely on, each refused with its own error code:

* the request must still be open: in progress (its active stage and the stages
  after it) or returned to its requester (the stages it will pass through
  again, starting with the one that returned it);
* a reason is required, and kept to 500 characters;
* a new approver is an active member of the request's tenant, able to work in
  the request's branch when it has one, and not somebody the document's own
  rule keeps from deciding it (the requester, or the person a leave request is
  for), unless the document type lets its requester approve;
* somebody who has already decided the stage's current attempt stays on it;
  undoing their decision is the separate reversal action;
* a stage keeps at least one person, and a stage that needs a quorum keeps at
  least that many.

Under a stage everybody must approve, the count follows the list. Taking off
the last person still to decide leaves everybody remaining already approved,
and the stage completes through the same path a vote takes
(:func:`vs_workflow.services.actions.complete_stage_if_approved`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from vs_workflow.constants import (
    NOTIF_EVENT_APPROVER_REMOVED, NOTIF_EVENT_STAGE_ACTIVATED,
    ApproverChangeKind, AuditEventType, StageAdvanceRule, StageKind,
    WorkflowInstanceStatus, WorkflowStageStatus,
)
from vs_workflow.exceptions import (
    ApproverAlreadyVotedError, ApproverConflictError, ApproverOutOfReachError,
    InstanceNotOpenError, ReasonRequiredError, ReassignmentError,
    StageEmptyError, StageNotChangeableError, StageNotActiveError, WorkflowError,
)
from vs_workflow.models import (
    ApprovalDelegation, WorkflowApproverChange, WorkflowInstance, WorkflowStage,
    WorkflowStageAction, WorkflowStageApprover, WorkflowStageAssignment,
    WorkflowStageInstance,
)
from vs_workflow.services import approvers as approvers_service
from vs_workflow.services import audit as audit_service
from vs_workflow.services.actions import (
    _active_stage_instance, _live_votes, _lock_instance, complete_stage_if_approved,
)
from vs_workflow.services.routing import notify

REASON_MAX_LENGTH = 500

ACTIVE, DONE, UPCOMING = "ACTIVE", "DONE", "UPCOMING"

#: Statuses whose approvers may still change.
OPEN_STATUSES = (WorkflowInstanceStatus.IN_PROGRESS, WorkflowInstanceStatus.RETURNED)

#: A stage attempt some decision has closed.
_DECIDED_STAGE_STATUSES = (
    WorkflowStageStatus.APPROVED, WorkflowStageStatus.REJECTED, WorkflowStageStatus.RETURNED,
)

_NOT_OPEN_MESSAGES = {
    WorkflowInstanceStatus.APPROVED: "This request has been approved, so its approvers can no longer be changed.",
    WorkflowInstanceStatus.REJECTED: "This request has been rejected, so its approvers can no longer be changed.",
    WorkflowInstanceStatus.WITHDRAWN: "This request was withdrawn, so its approvers can no longer be changed.",
    WorkflowInstanceStatus.CANCELLED: "This request was cancelled, so its approvers can no longer be changed.",
}


# --------------------------------------------------------------------------- #
# Where each stage of a request stands                                        #
# --------------------------------------------------------------------------- #

@dataclass
class StageState:
    """One approval stage of a request and whether it is done, waiting or still to come."""

    stage: WorkflowStage
    state: str
    stage_instance: Optional[WorkflowStageInstance]


def stage_states(instance: WorkflowInstance) -> list:
    """The request's approval stages in template order, each marked DONE, ACTIVE or UPCOMING.

    ACTIVE is the stage an in-progress request is waiting on. DONE is a stage
    whose latest attempt some decision closed. UPCOMING is every approval stage
    still to come after the current one in template order, and for a returned
    request also the stage that returned it, which opens again on
    resubmission. Routing conditions may still skip an upcoming stage when the
    request gets there. A stage the request passed without anybody deciding
    it (skipped, or routed around) is none of the three and is left out, as is
    a retired stage the request never ran.
    """
    stages = [
        s for s in instance.template.stages.order_by("order", "pk")
        if s.kind == StageKind.APPROVAL
    ]
    latest = {}
    for si in WorkflowStageInstance.objects.filter(instance=instance).order_by("attempt"):
        latest[si.stage_id] = si
    current = instance.current_stage
    current_order = current.order if current is not None else None
    is_open = instance.status in OPEN_STATUSES

    rows = []
    for stage in stages:
        si = latest.get(stage.pk)
        if is_open and stage.pk == instance.current_stage_id:
            if (instance.status == WorkflowInstanceStatus.IN_PROGRESS
                    and si is not None and si.status == WorkflowStageStatus.ACTIVE):
                rows.append(StageState(stage, ACTIVE, si))
                continue
            if instance.status == WorkflowInstanceStatus.RETURNED and stage.retired_at is None:
                rows.append(StageState(stage, UPCOMING, si))
                continue
        if si is not None and si.status in _DECIDED_STAGE_STATUSES:
            rows.append(StageState(stage, DONE, si))
            continue
        if (is_open and current_order is not None and stage.retired_at is None
                and stage.order > current_order):
            rows.append(StageState(stage, UPCOMING, si))
    return rows


def blocked_reason(instance: WorkflowInstance) -> Optional[str]:
    """Why nobody may change this request's approvers, or None when they may."""
    if instance.status in OPEN_STATUSES:
        return None
    return _NOT_OPEN_MESSAGES.get(
        instance.status, "This request is not waiting on anybody, so its approvers cannot be changed.",
    )


def _assert_open(instance: WorkflowInstance) -> None:
    reason = blocked_reason(instance)
    if reason is not None:
        raise InstanceNotOpenError(reason, status=instance.status)


def _changeable_stage(instance: WorkflowInstance, stage_id) -> StageState:
    """The stage *stage_id* of an open request, refusing one that is done or not ahead."""
    _assert_open(instance)
    for row in stage_states(instance):
        if str(row.stage.pk) != str(stage_id):
            continue
        if row.state == DONE:
            raise StageNotChangeableError(
                f"{row.stage.label} has already been decided, so its approvers "
                f"can no longer be changed.", stage=str(row.stage.pk),
            )
        return row
    raise StageNotChangeableError(
        "That stage is not one this request is still waiting on or still to pass through.",
        stage=str(stage_id),
    )


# --------------------------------------------------------------------------- #
# People                                                                      #
# --------------------------------------------------------------------------- #

def person_name(user) -> str:
    """The name screens show for *user*, falling back to their email."""
    if user is None:
        return ""
    return (getattr(user, "full_name", "") or "").strip() or user.email


def person(user) -> Optional[dict]:
    """``{"id", "name"}`` for *user*, or None."""
    if user is None:
        return None
    return {"id": user.pk, "name": person_name(user)}


def clean_reason(reason) -> str:
    """The reason as it will be kept, refusing a blank one or one past 500 characters."""
    text = (reason or "").strip() if isinstance(reason, str) else ""
    if not text:
        raise ReasonRequiredError("Say why the approvers are changing.")
    if len(text) > REASON_MAX_LENGTH:
        raise ReasonRequiredError(
            f"Keep the reason to {REASON_MAX_LENGTH} characters or fewer.",
        )
    return text


def load_people(tenant_id, user_ids) -> dict:
    """``{id: user}`` for *user_ids*, all members of the tenant, in any status.

    An id that names nobody in the tenant is refused without saying whether it
    names somebody elsewhere: the answer to "is that a person in another
    tenant" is itself the disclosure.
    """
    ids = set()
    for value in user_ids:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            raise ApproverOutOfReachError("One of the people chosen cannot approve requests here.")
    users = {
        u.pk: u for u in get_user_model().objects.filter(pk__in=ids, tenant_id=tenant_id)
    }
    if ids - users.keys():
        raise ApproverOutOfReachError("One of the people chosen cannot approve requests here.")
    return users


def _reaches_branch(user, instance: WorkflowInstance) -> bool:
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    if instance.branch_id is None:
        return True
    reach = visible_branch_ids(user, instance.tenant)
    return reach is WHOLE_TENANT or instance.branch_id in reach


def check_new_approver(user, instance: WorkflowInstance, conflicts=None) -> None:
    """Refuse *user* as a new approver on *instance*, naming why.

    Membership is the same rule every approver source is held to
    (``approvers._tenant_members``): an active account in the request's own
    tenant. Branch reach is the same answer every screen narrows by
    (:func:`vs_rbac.scoping.visible_branch_ids`): a person who works across the
    whole tenant reaches every branch.
    """
    name = person_name(user)
    if not approvers_service._tenant_members([user], instance.tenant_id):
        raise ApproverOutOfReachError(
            f"{name} does not have an active account here, so they cannot approve this request.",
            user=user.pk,
        )
    if not _reaches_branch(user, instance):
        branch = getattr(instance.branch, "name", "") or "this branch"
        raise ApproverOutOfReachError(
            f"{name} does not work in {branch}, so they cannot approve a request from it.",
            user=user.pk,
        )
    if not approvers_service.may_decide(user, instance, conflicts):
        raised = user.pk in approvers_service.requester_ids(instance)
        why = "they raised it" if raised else "it is about them"
        raise ApproverConflictError(
            f"{name} cannot approve this request, because {why}.", user=user.pk,
        )


def _check_enough(stage: WorkflowStage, user_ids) -> None:
    if not user_ids:
        raise StageEmptyError(
            f"{stage.label} must keep at least one approver.", stage=str(stage.pk),
        )
    needed = stage.quorum_count or 1
    if stage.advance_rule == StageAdvanceRule.QUORUM and len(user_ids) < needed:
        raise StageEmptyError(
            f"{stage.label} needs {needed} approvals, so it must keep at least "
            f"{needed} people.", stage=str(stage.pk),
        )


def preview_approvers(stage: WorkflowStage, instance: WorkflowInstance) -> list:
    """Who would approve *stage* if it opened now, as the engine would resolve it.

    A stage the resolver refuses to resolve (an approver source it does not
    know) previews as nobody rather than failing the screen.
    """
    try:
        return approvers_service.resolve_approvers(stage, instance)
    except WorkflowError:
        return []


# --------------------------------------------------------------------------- #
# Recording a change                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class _Seat:
    """One place on a stage: a person, and who they act for when they are a delegate."""

    user: object
    on_behalf_of: object = None


def _record(instance, *, kind, stage, stage_instance, removed, added, reason, actor,
            delegation=None) -> None:
    """Write the history rows for one change.

    A person in their own right taken off is paired with one put on, so a swap
    reads as one row naming both. Everything else (a delegate, or an addition
    or removal with no counterpart) is a row of its own.
    """
    own_removed = [s for s in removed if s.on_behalf_of is None]
    own_added = [s for s in added if s.on_behalf_of is None]
    pairs = list(zip(own_removed, own_added))
    paired = {id(s) for pair in pairs for s in pair}
    rows = [
        WorkflowApproverChange(removed_user=r.user, added_user=a.user) for r, a in pairs
    ]
    rows += [
        WorkflowApproverChange(removed_user=s.user, on_behalf_of=s.on_behalf_of)
        for s in removed if id(s) not in paired
    ]
    rows += [
        WorkflowApproverChange(added_user=s.user, on_behalf_of=s.on_behalf_of)
        for s in added if id(s) not in paired
    ]
    for row in rows:
        row.instance = instance
        row.kind = kind
        row.stage = stage
        row.stage_instance = stage_instance
        row.delegation = delegation
        row.reason = reason
        row.changed_by = actor
    WorkflowApproverChange.objects.bulk_create(rows)


def _audit(instance, event, *, kind, stage, stage_instance, removed, added, reason, actor,
           **extra) -> None:
    audit_service.write(
        instance, event, actor=actor, stage_instance=stage_instance,
        context={
            "kind": kind,
            "stage_code": stage.code,
            "stage_label": stage.label,
            "removed": [{"user_id": s.user.pk, "on_behalf_of": getattr(s.on_behalf_of, "pk", None)}
                        for s in removed],
            "added": [{"user_id": s.user.pk, "on_behalf_of": getattr(s.on_behalf_of, "pk", None)}
                      for s in added],
            "reason": reason,
            **extra,
        },
    )


def _tell(instance, stage, *, added_ids=(), removed_ids=()) -> None:
    """Tell people put on a waiting stage, and people taken off it."""
    context = {"stage_name": stage.label, "stage_label": stage.label}
    if added_ids:
        notify(instance, NOTIF_EVENT_STAGE_ACTIVATED,
               recipient_user_ids=sorted({str(i) for i in added_ids}),
               context=context | {"submitter_name": instance.requested_by.full_name})
    if removed_ids:
        notify(instance, NOTIF_EVENT_APPROVER_REMOVED,
               recipient_user_ids=sorted({str(i) for i in removed_ids}),
               context=context)


# --------------------------------------------------------------------------- #
# The waiting stage                                                           #
# --------------------------------------------------------------------------- #

def _snapshot(stage_instance) -> list:
    return list(
        WorkflowStageApprover.objects
        .filter(stage_instance=stage_instance, attempt=stage_instance.attempt)
        .select_related("user", "on_behalf_of")
        .order_by("recorded_at", "pk")
    )


def _seats_for(instance, users, taken, conflicts) -> list:
    """The places *users* take on a waiting stage, with their active delegates.

    Mirrors activation: each person's active delegate joins beside them, or in
    their place when the delegation is exclusive (see
    :func:`vs_workflow.services.approvers.resolve_approvers`). ``taken`` is the
    set of ``(user id, on behalf of id)`` places already on the stage, which is
    extended with each new place so nobody sits twice.
    """
    delegations = approvers_service.active_delegations(
        instance, [u.pk for u in users], conflicts=conflicts,
    )
    exclusive = {d.delegator_id for d in delegations if d.exclusive}
    seats = []
    for user in users:
        if user.pk in exclusive or (user.pk, None) in taken:
            continue
        taken.add((user.pk, None))
        seats.append(_Seat(user))
    for d in delegations:
        if (d.delegate_id, d.delegator_id) in taken:
            continue
        taken.add((d.delegate_id, d.delegator_id))
        seats.append(_Seat(d.delegate, d.delegator))
    return seats


def _voted_verb(action: str) -> str:
    return {"APPROVED": "approved", "REJECTED": "rejected", "RETURNED": "returned"}.get(
        action, "decided")


def _set_active_list(instance, stage_instance, stage, people: dict, *, reason, actor,
                     kind) -> bool:
    """Make the waiting stage's list exactly *people*. Returns whether anything changed.

    The diff is by person: somebody on the list keeps every place they hold,
    including as a delegate, and somebody off it loses every one.
    """
    rows = _snapshot(stage_instance)
    live = {r.user_id for r in rows}
    wanted = set(people)
    removed_ids = live - wanted
    added_ids = wanted - live
    if not removed_ids and not added_ids:
        return False
    _check_enough(stage, wanted)

    vote = (_live_votes(stage_instance).filter(actor_id__in=removed_ids)
            .select_related("actor").order_by("acted_at").first())
    if vote is not None:
        raise ApproverAlreadyVotedError(
            f"{person_name(vote.actor)} has already {_voted_verb(vote.action)} "
            f"{stage.label}, so they cannot be removed. Reverse their decision "
            f"first if it should not stand.", user=vote.actor_id,
        )
    conflicts = approvers_service.approval_conflict_ids(instance)
    for user_id in sorted(added_ids):
        check_new_approver(people[user_id], instance, conflicts)

    gone = [r for r in rows if r.user_id in removed_ids]
    WorkflowStageApprover.objects.filter(pk__in=[r.pk for r in gone]).delete()
    taken = {(r.user_id, r.on_behalf_of_id) for r in rows if r.user_id not in removed_ids}
    seats = _seats_for(instance, [people[i] for i in sorted(added_ids)], taken, conflicts)
    WorkflowStageApprover.objects.bulk_create([
        WorkflowStageApprover(stage_instance=stage_instance, user=s.user,
                              on_behalf_of=s.on_behalf_of, attempt=stage_instance.attempt)
        for s in seats
    ])

    removed = [_Seat(r.user, r.on_behalf_of) for r in gone]
    _record(instance, kind=kind, stage=stage, stage_instance=stage_instance,
            removed=removed, added=seats, reason=reason, actor=actor)
    _audit(instance, AuditEventType.APPROVERS_CHANGED, kind=kind, stage=stage,
           stage_instance=stage_instance, removed=removed, added=seats,
           reason=reason, actor=actor)
    _tell(instance, stage, added_ids={s.user.pk for s in seats}, removed_ids=removed_ids)
    complete_stage_if_approved(instance, stage_instance)
    return True


# --------------------------------------------------------------------------- #
# A stage not yet open                                                        #
# --------------------------------------------------------------------------- #

def _set_upcoming(instance, stage, people: dict, *, reason, actor, kind) -> bool:
    """Choose *people* in advance for a stage that has not opened. Returns whether anything changed.

    The change is measured against what the stage would have used: the
    previous choice, or else who would approve it if it opened now.
    """
    assignment = (WorkflowStageAssignment.objects.select_for_update()
                  .filter(instance=instance, stage=stage).first())
    if assignment is not None:
        before = {u.pk: u for u in assignment.approvers.all()}
    else:
        before = {e.user.pk: e.user for e in preview_approvers(stage, instance)}
    wanted = set(people)
    removed_ids = before.keys() - wanted
    added_ids = wanted - before.keys()
    if assignment is not None and not removed_ids and not added_ids:
        return False
    _check_enough(stage, wanted)
    conflicts = approvers_service.approval_conflict_ids(instance)
    for user_id in sorted(added_ids):
        check_new_approver(people[user_id], instance, conflicts)

    now = timezone.now()
    if assignment is None:
        assignment = WorkflowStageAssignment.objects.create(
            instance=instance, stage=stage, reason=reason, set_by=actor, set_at=now,
        )
    else:
        assignment.reason, assignment.set_by, assignment.set_at = reason, actor, now
        assignment.save(update_fields=["reason", "set_by", "set_at"])
    assignment.approvers.set(list(people.values()))

    removed = [_Seat(before[i]) for i in sorted(removed_ids)]
    added = [_Seat(people[i]) for i in sorted(added_ids)]
    _record(instance, kind=kind, stage=stage, stage_instance=None,
            removed=removed, added=added, reason=reason, actor=actor)
    _audit(instance, AuditEventType.APPROVERS_ASSIGNED, kind=kind, stage=stage,
           stage_instance=None, removed=removed, added=added, reason=reason, actor=actor,
           approvers=sorted(people))
    return True


# --------------------------------------------------------------------------- #
# Administrator entry points                                                  #
# --------------------------------------------------------------------------- #

def set_stage_approvers(instance_id, *, stage_id, user_ids: Iterable, reason, actor) -> WorkflowInstance:
    """Make *user_ids* the complete list of people for one stage of one request.

    For the stage the request is waiting on, the live list changes now; for a
    stage still to come, the choice is kept and used when it opens. The caller
    has already established that *actor* may reach this request.
    """
    reason = clean_reason(reason)
    with transaction.atomic():
        instance = _lock_instance(instance_id)
        row = _changeable_stage(instance, stage_id)
        people = load_people(instance.tenant_id, user_ids)
        if row.state == ACTIVE:
            _set_active_list(instance, row.stage_instance, row.stage, people,
                             reason=reason, actor=actor, kind=ApproverChangeKind.ACTIVE_STAGE)
        else:
            _set_upcoming(instance, row.stage, people, reason=reason, actor=actor,
                          kind=ApproverChangeKind.UPCOMING_ASSIGNMENT)
    return instance


def reset_stage_assignment(instance_id, *, stage_id, reason, actor) -> WorkflowInstance:
    """Drop an upcoming stage's advance choice, so it resolves its approvers normally when it opens."""
    reason = clean_reason(reason)
    with transaction.atomic():
        instance = _lock_instance(instance_id)
        row = _changeable_stage(instance, stage_id)
        if row.state != UPCOMING:
            raise StageNotChangeableError(
                f"{row.stage.label} is already open. Change its approvers directly instead.",
                stage=str(row.stage.pk),
            )
        assignment = (WorkflowStageAssignment.objects.select_for_update()
                      .filter(instance=instance, stage=row.stage).first())
        if assignment is None:
            raise StageNotChangeableError(
                f"Nobody has been chosen in advance for {row.stage.label}, so there "
                f"is nothing to reset.", stage=str(row.stage.pk),
            )
        removed = [_Seat(u) for u in assignment.approvers.order_by("pk")]
        assignment.delete()
        _record(instance, kind=ApproverChangeKind.ASSIGNMENT_RESET, stage=row.stage,
                stage_instance=None, removed=removed, added=[], reason=reason, actor=actor)
        _audit(instance, AuditEventType.APPROVERS_ASSIGNMENT_RESET,
               kind=ApproverChangeKind.ASSIGNMENT_RESET, stage=row.stage,
               stage_instance=None, removed=removed, added=[], reason=reason, actor=actor)
    return instance


def _replace_on_one(instance_id, from_user, to_user, reason, actor) -> str:
    """Replace *from_user* with *to_user* on one request. Returns a note on anything left.

    Upcoming choices are changed before the waiting stage, because completing
    the waiting stage can open the next one, and the next one must open with
    the new person already chosen.
    """
    instance = _lock_instance(instance_id)
    _assert_open(instance)
    changed, refusals = False, []
    states = stage_states(instance)
    ordered = [r for r in states if r.state == UPCOMING] + [r for r in states if r.state == ACTIVE]
    for row in ordered:
        if row.state == UPCOMING:
            assignment = WorkflowStageAssignment.objects.filter(
                instance=instance, stage=row.stage).first()
            if assignment is None:
                continue
            current = {u.pk: u for u in assignment.approvers.all()}
            if from_user.pk not in current:
                continue
        else:
            current = {}
            for snap in _snapshot(row.stage_instance):
                current.setdefault(snap.user_id, snap.user)
            if from_user.pk not in current:
                continue
        people = {k: v for k, v in current.items() if k != from_user.pk}
        people[to_user.pk] = to_user
        try:
            with transaction.atomic():
                if row.state == UPCOMING:
                    _set_upcoming(instance, row.stage, people, reason=reason, actor=actor,
                                  kind=ApproverChangeKind.BULK_REPLACE)
                else:
                    _set_active_list(instance, row.stage_instance, row.stage, people,
                                     reason=reason, actor=actor,
                                     kind=ApproverChangeKind.BULK_REPLACE)
            changed = True
        except ReassignmentError as exc:
            refusals.append(exc.message)
    if not changed:
        raise ReassignmentError(
            refusals[0] if refusals
            else f"{person_name(from_user)} is not waiting on this request.",
        )
    return refusals[0] if refusals else ""


def replace_approver(instance_ids, *, from_user_id, to_user_id, reason, actor, tenant,
                     reachable_ids) -> dict:
    """Replace one person with another across many requests, skipping those it cannot.

    ``reachable_ids`` is the set of request ids the caller may reach; any other
    id is skipped as not found, so the answer never says whether a request
    exists outside the caller's reach. Each request is changed in its own
    transaction, so one refusal leaves the others to go through.

    *from_user* may be anybody in the tenant, including somebody whose account
    has since closed (the commonest reason to move their work). *to_user* must
    be somebody who can approve here at all; whether they can approve each
    request is decided request by request.
    """
    reason = clean_reason(reason)
    people = load_people(tenant.pk, [from_user_id, to_user_id])
    from_user, to_user = people[int(from_user_id)], people[int(to_user_id)]
    if from_user.pk == to_user.pk:
        raise ReassignmentError("Choose somebody else to hand these requests to.")
    if not approvers_service._tenant_members([to_user], tenant.pk):
        raise ApproverOutOfReachError(
            f"{person_name(to_user)} does not have an active account here, so they "
            f"cannot approve requests.", user=to_user.pk,
        )

    results, seen = [], set()
    for instance_id in instance_ids:
        instance_id = str(instance_id)
        if instance_id in seen:
            continue
        seen.add(instance_id)
        if instance_id not in reachable_ids:
            results.append({"instance_id": instance_id, "outcome": "skipped",
                            "detail": "This request was not found."})
            continue
        try:
            with transaction.atomic():
                detail = _replace_on_one(instance_id, from_user, to_user, reason, actor)
        except ReassignmentError as exc:
            results.append({"instance_id": instance_id, "outcome": "skipped",
                            "detail": exc.message})
            continue
        results.append({"instance_id": instance_id, "outcome": "replaced", "detail": detail})
    replaced = sum(1 for r in results if r["outcome"] == "replaced")
    return {"replaced": replaced, "skipped": len(results) - replaced, "results": results}


# --------------------------------------------------------------------------- #
# Delegations reaching, and leaving, waiting requests                         #
# --------------------------------------------------------------------------- #

def _waiting_places(delegation, *, user_id, on_behalf_of_id):
    """Instance ids with a place for ``(user_id, on_behalf_of_id)`` on their waiting stage."""
    qs = WorkflowStageApprover.objects.filter(
        user_id=user_id,
        stage_instance__status=WorkflowStageStatus.ACTIVE,
        stage_instance__instance__status=WorkflowInstanceStatus.IN_PROGRESS,
        stage_instance__instance__tenant_id=delegation.tenant_id,
        attempt=F("stage_instance__attempt"),
    )
    qs = (qs.filter(on_behalf_of__isnull=True) if on_behalf_of_id is None
          else qs.filter(on_behalf_of_id=on_behalf_of_id))
    if delegation.document_type:
        qs = qs.filter(stage_instance__instance__document_type=delegation.document_type)
    return list(
        qs.order_by().values_list("stage_instance__instance_id", flat=True).distinct()
    )


def _waiting_stage(instance):
    try:
        return _active_stage_instance(instance)
    except StageNotActiveError:
        return None


def _apply_to_one(instance_id, delegation, actor) -> bool:
    instance = _lock_instance(instance_id)
    if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
        return False
    si = _waiting_stage(instance)
    if si is None:
        return False
    delegator_id, delegate_id = delegation.delegator_id, delegation.delegate_id
    rows = _snapshot(si)
    own = [r for r in rows if r.user_id == delegator_id and r.on_behalf_of_id is None]
    if not own or any(r.user_id == delegate_id and r.on_behalf_of_id == delegator_id
                      for r in rows):
        return False
    if _live_votes(si).filter(Q(actor_id=delegator_id) | Q(proxied_by_id=delegator_id)).exists():
        return False
    delegate = delegation.delegate
    if not (approvers_service._tenant_members([delegate], instance.tenant_id)
            and approvers_service.may_decide(delegate, instance)):
        return False

    WorkflowStageApprover.objects.create(
        stage_instance=si, user=delegate, on_behalf_of=delegation.delegator, attempt=si.attempt,
    )
    removed = []
    if delegation.exclusive:
        WorkflowStageApprover.objects.filter(pk__in=[r.pk for r in own]).delete()
        removed = [_Seat(delegation.delegator)]
    added = [_Seat(delegate, delegation.delegator)]
    kind = ApproverChangeKind.DELEGATION_APPLIED
    _record_delegation(instance, si, kind, delegation, actor, delegator_moved=bool(removed))
    _audit(instance, AuditEventType.DELEGATION_APPLIED, kind=kind, stage=si.stage,
           stage_instance=si, removed=removed, added=added, reason=delegation.reason,
           actor=actor, delegation_id=str(delegation.pk))
    _tell(instance, si.stage, added_ids={delegate_id},
          removed_ids={delegator_id} if removed else set())
    return True


def _record_delegation(instance, si, kind, delegation, actor, *, delegator_moved: bool) -> None:
    """One history row for a delegation reaching or leaving a waiting stage.

    Applied: the delegate is added, on behalf of the delegator, and an
    exclusive delegation removes the delegator in the same row. Revoked: the
    delegate is removed, and the delegator added back where they return.
    """
    applied = kind == ApproverChangeKind.DELEGATION_APPLIED
    delegator = delegation.delegator if delegator_moved else None
    WorkflowApproverChange.objects.create(
        instance=instance, kind=kind, stage=si.stage, stage_instance=si,
        added_user=delegation.delegate if applied else delegator,
        removed_user=delegator if applied else delegation.delegate,
        on_behalf_of=delegation.delegator, delegation=delegation,
        reason=delegation.reason, changed_by=actor,
    )


def apply_delegation(delegation: ApprovalDelegation, *, actor=None, now=None) -> int:
    """Put a delegation that is running now on the requests already waiting on its delegator.

    A request qualifies when its waiting stage lists the delegator in their own
    right on the current attempt, the delegator has not yet decided it, and
    the delegation covers the request's document type. The delegate joins with
    "on behalf of" the delegator recorded, beside them, or in their place for
    an exclusive delegation, exactly as at activation. Stamps ``applied_at``.
    Returns how many requests it reached.

    Idempotent: a request that already carries the delegate for this delegator
    is left alone, so a sweep that runs twice, or a stage that opened after the
    delegation started, does not seat anybody twice.
    """
    now = now or timezone.now()
    if delegation.revoked_at is not None or not (delegation.starts_at <= now <= delegation.ends_at):
        return 0
    reached = 0
    for instance_id in _waiting_places(delegation, user_id=delegation.delegator_id,
                                       on_behalf_of_id=None):
        with transaction.atomic():
            reached += _apply_to_one(instance_id, delegation, actor)
    ApprovalDelegation.all_objects.filter(pk=delegation.pk).update(applied_at=now)
    delegation.applied_at = now
    return reached


def _withdraw_from_one(instance_id, delegation, actor) -> bool:
    instance = _lock_instance(instance_id)
    if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
        return False
    si = _waiting_stage(instance)
    if si is None:
        return False
    delegator_id, delegate_id = delegation.delegator_id, delegation.delegate_id
    rows = _snapshot(si)
    theirs = [r for r in rows if r.user_id == delegate_id and r.on_behalf_of_id == delegator_id]
    if not theirs:
        return False
    if _live_votes(si).filter(actor_id=delegate_id).exists():
        return False

    restore = False
    if delegation.exclusive and not any(
            r.user_id == delegator_id and r.on_behalf_of_id is None for r in rows):
        # Another exclusive delegation of theirs still holds this stage.
        seated = {(r.user_id, r.on_behalf_of_id) for r in rows}
        still_away = any(
            d.exclusive and d.pk != delegation.pk and (d.delegate_id, delegator_id) in seated
            for d in approvers_service.active_delegations(instance, [delegator_id])
        )
        delegator = delegation.delegator
        restore = (not still_away
                   and bool(approvers_service._tenant_members([delegator], instance.tenant_id))
                   and approvers_service.may_decide(delegator, instance))
    if len(rows) - len(theirs) + restore == 0:
        # Never leave a waiting stage with nobody on it.
        return False

    WorkflowStageApprover.objects.filter(pk__in=[r.pk for r in theirs]).delete()
    added = []
    if restore:
        WorkflowStageApprover.objects.create(
            stage_instance=si, user=delegation.delegator, attempt=si.attempt,
        )
        added = [_Seat(delegation.delegator)]
    removed = [_Seat(delegation.delegate, delegation.delegator)]
    kind = ApproverChangeKind.DELEGATION_REVOKED
    _record_delegation(instance, si, kind, delegation, actor, delegator_moved=restore)
    _audit(instance, AuditEventType.DELEGATION_WITHDRAWN, kind=kind, stage=si.stage,
           stage_instance=si, removed=removed, added=added, reason=delegation.reason,
           actor=actor, delegation_id=str(delegation.pk))
    _tell(instance, si.stage, added_ids={delegator_id} if restore else set(),
          removed_ids={delegate_id})
    complete_stage_if_approved(instance, si)
    return True


def withdraw_delegation(delegation: ApprovalDelegation, *, actor=None) -> int:
    """Take a revoked delegation's delegate off the requests still waiting on them.

    Only where the delegate has not decided the stage: a decision already made
    stands, and undoing it is the reversal action. Where the delegation was
    exclusive and took the delegator off, the delegator is put back, unless
    another exclusive delegation of theirs still holds the stage. A stage that
    would be left with nobody keeps the delegate. Returns how many requests it
    changed.

    Call it after ``revoked_at`` is set, so the delegation no longer counts as
    active while the delegator's other delegations are weighed.
    """
    changed = 0
    for instance_id in _waiting_places(delegation, user_id=delegation.delegate_id,
                                       on_behalf_of_id=delegation.delegator_id):
        with transaction.atomic():
            changed += _withdraw_from_one(instance_id, delegation, actor)
    return changed


def apply_started_delegations(now=None) -> int:
    """Carry every delegation that has started, and not yet reached waiting requests, to them.

    Run by the periodic sweep (``vs_workflow.apply_started_delegations``), so a
    delegation set up today to start on Monday reaches the requests waiting on
    its delegator on Monday. Returns how many requests were reached.
    """
    now = now or timezone.now()
    due = (ApprovalDelegation.all_objects
           .filter(applied_at__isnull=True, revoked_at__isnull=True,
                   starts_at__lte=now, ends_at__gte=now)
           .select_related("delegator", "delegate")
           .order_by("starts_at", "pk"))
    return sum(apply_delegation(d, actor=None, now=now) for d in due)


# --------------------------------------------------------------------------- #
# What the administrator's screen reads                                       #
# --------------------------------------------------------------------------- #

def approver_overview(instance: WorkflowInstance) -> dict:
    """Every approval stage of a request with who approves it, plus the change history.

    The body of ``GET /workflow/instances/{id}/approvers/``: DONE and ACTIVE
    stages list the people on their latest attempt with each one's decision;
    an UPCOMING stage lists the administrator's advance choice when there is
    one, and otherwise who would approve it if it opened now (``preview``).
    """
    states = stage_states(instance)
    is_open = instance.status in OPEN_STATUSES
    seated = [r.stage_instance for r in states if r.state in (DONE, ACTIVE)]

    snaps_by_si = {}
    for snap in (WorkflowStageApprover.objects
                 .filter(stage_instance__in=seated)
                 .select_related("user", "on_behalf_of", "stage_instance")
                 .order_by("recorded_at", "pk")):
        if snap.attempt == snap.stage_instance.attempt:
            snaps_by_si.setdefault(snap.stage_instance_id, []).append(snap)
    votes = {}
    for action in (WorkflowStageAction.objects
                   .filter(stage_instance__in=seated, reversed_at__isnull=True,
                           is_reversal_of__isnull=True)
                   .select_related("stage_instance")):
        if action.attempt == action.stage_instance.attempt:
            votes[(action.stage_instance_id, action.actor_id)] = action.action
    assignments = {
        a.stage_id: a for a in WorkflowStageAssignment.objects
        .filter(instance=instance).select_related("set_by").prefetch_related("approvers")
    }

    stages = []
    for row in states:
        stage = row.stage
        entry = {
            "stage_id": str(stage.pk),
            "label": stage.label,
            "order": stage.order,
            "state": row.state,
            "advance_rule": stage.advance_rule,
            "quorum_count": (stage.quorum_count or 1)
            if stage.advance_rule == StageAdvanceRule.QUORUM else None,
            "approvers": [],
            "preview": False,
            "assignment": None,
            "may_change": is_open and row.state != DONE,
        }
        if row.state in (DONE, ACTIVE):
            entry["approvers"] = [
                {**person(s.user), "on_behalf_of": person(s.on_behalf_of),
                 "vote": votes.get((row.stage_instance.pk, s.user_id))}
                for s in snaps_by_si.get(row.stage_instance.pk, [])
            ]
        else:
            assignment = assignments.get(stage.pk)
            if assignment is not None:
                chosen = sorted(assignment.approvers.all(), key=lambda u: person_name(u).lower())
                entry["assignment"] = {
                    "approvers": [person(u) for u in chosen],
                    "reason": assignment.reason,
                    "set_by": person(assignment.set_by),
                    "set_at": assignment.set_at,
                }
                entry["approvers"] = [
                    {**person(u), "on_behalf_of": None, "vote": None} for u in chosen
                ]
            else:
                entry["preview"] = True
                entry["approvers"] = [
                    {**person(e.user), "on_behalf_of": person(e.on_behalf_of), "vote": None}
                    for e in preview_approvers(stage, instance)
                ]
        stages.append(entry)

    history = [
        {
            "kind": change.kind,
            "stage_id": str(change.stage_id),
            "stage_label": change.stage.label,
            "removed": person(change.removed_user),
            "added": person(change.added_user),
            "on_behalf_of": person(change.on_behalf_of),
            "reason": change.reason,
            "by": person(change.changed_by),
            "at": change.changed_at,
        }
        for change in (WorkflowApproverChange.objects.filter(instance=instance)
                       .select_related("stage", "removed_user", "added_user",
                                       "on_behalf_of", "changed_by")
                       .order_by("-changed_at", "-pk"))
    ]
    return {
        "instance_id": str(instance.pk),
        "may_change": is_open,
        "blocked_reason": blocked_reason(instance),
        "stages": stages,
        "history": history,
    }
