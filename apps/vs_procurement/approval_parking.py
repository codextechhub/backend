"""Parked spend approvals: detect them, and make them reachable again.

A procurement stage that activates while nobody holds its approving permission is
**parked**: the stage is ACTIVE, the instance is IN_PROGRESS, and its frozen approver
snapshot (``WorkflowStageApprover``) is empty. That is the deliberate outcome of
``skip_if_no_approvers=False`` on the seeded procurement stages - spend must never
approve itself.

Parking on its own would be a trap. ``vs_workflow`` resolves eligibility exactly once,
at stage activation (``routing._activate_stage``), and every read path afterwards -
``actions._check_eligibility``, ``actions._stage_fully_approved``,
``my_queue.pending_approval_snapshots`` and Procurement's own inbox - consults that
frozen snapshot rather than live RBAC. A stage activated with **zero** eligible
approvers is therefore permanently unreachable for that attempt: granting somebody the
permission afterwards changes nothing, and only a return-to-requester + resubmit (a new
attempt) would re-snapshot.

This module is the procurement-side repair for exactly that state. ``vs_workflow`` is
shared with finance approvals and payout batches and is not modified.

What the repair may do
    Fill an **empty** approver snapshot on an ACTIVE procurement stage by re-resolving
    approvers with the engine's own resolver, under a row lock.

What it must never do
    * Touch a populated snapshot. The freeze guarantee protects an approver who is
      mid-review from having their eligibility rewritten under them; the emptiness
      check is re-run *inside* the lock and is the hard precondition for any write.
    * Approve, advance, skip, or otherwise move a workflow. It restores reachability
      and nothing else - the decision still needs a human.
    * Reach outside procurement. Every query is bounded to
      :data:`~vs_procurement.constants.PROCUREMENT_APPROVAL_TYPES`.

When nobody can be granted the permission in time, the last resort is
:mod:`vs_procurement.approval_override`, which builds on the same locked "is it parked"
predicate defined here (:func:`lock_parked_stage`) so the two can never disagree.

Because the repair only ever fires on a snapshot of size zero, it cannot change a
stage's advance arithmetic retroactively: an ``ANY`` stage with no approvers had no
possible vote, and a ``UNANIMOUS`` stage requires ``eligible_count > 0`` before it can
advance at all.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, OuterRef

from vs_workflow.constants import (
    ApproverScope,
    ApproverSource,
    AuditEventType,
    WorkflowInstanceStatus,
    WorkflowStageStatus,
)
from vs_workflow.models import WorkflowStageApprover, WorkflowStageInstance
from vs_workflow.services import approvers as approvers_service
from vs_workflow.services import audit as audit_service

from .constants import PROCUREMENT_APPROVAL_TYPES, ProcApprovalState


# --------------------------------------------------------------------------- #
# Detection - a pure database predicate                                       #
# --------------------------------------------------------------------------- #

def _empty_active_stages():
    """ACTIVE procurement stages whose approver snapshot is empty for this attempt.

    One indexed query, no RBAC involvement: on healthy data it matches nothing and
    every caller short-circuits before resolving a single permission holder.
    """
    return WorkflowStageInstance.objects.filter(
        status=WorkflowStageStatus.ACTIVE,
        instance__status=WorkflowInstanceStatus.IN_PROGRESS,
        instance__document_type__in=PROCUREMENT_APPROVAL_TYPES,
    ).filter(
        ~Exists(WorkflowStageApprover.objects.filter(
            stage_instance=OuterRef("pk"), attempt=OuterRef("attempt"),
        )),
    ).select_related("stage", "instance")


def lock_parked_stage(stage_instance_id):
    """Lock one stage instance and return it only if it is *still* genuinely parked.

    The single in-transaction definition of "parked", shared by the lazy repair below and
    by the override in :mod:`vs_procurement.approval_override`, so the two can never
    disagree about what they are allowed to touch. Returns ``None`` when any precondition
    has moved underneath the caller - a repair staffed the stage, a vote landed, the
    instance went terminal - which the caller must treat as a refusal, not a retry.

    Must be called inside an open transaction; the lock is held until it commits.
    """
    stage_instance = (
        # ``of="self"`` locks only the stage-instance row: the joined instance and
        # branch are nullable sides of an outer join, which Postgres refuses to lock.
        WorkflowStageInstance.objects.select_for_update(of=("self",))
        .select_related("stage", "instance", "instance__tenant", "instance__branch")
        .filter(pk=stage_instance_id)
        .first()
    )
    if stage_instance is None:
        return None
    instance = stage_instance.instance
    if stage_instance.status != WorkflowStageStatus.ACTIVE:
        return None
    if instance.status != WorkflowInstanceStatus.IN_PROGRESS:
        return None
    if instance.document_type not in PROCUREMENT_APPROVAL_TYPES:
        return None
    # Emptiness is the whole precondition. A stage with even one eligible approver has a
    # human who can decide it: nothing to repair, and nothing for an override to release.
    if WorkflowStageApprover.objects.filter(
        stage_instance=stage_instance, attempt=stage_instance.attempt,
    ).exists():
        return None
    return stage_instance


# --------------------------------------------------------------------------- #
# Approver resolution with a per-request memo                                 #
# --------------------------------------------------------------------------- #

class _ResolutionCache:
    """Resolve stage approvers, sharing the RBAC holder lookup across stages.

    Several parked documents on one page usually share a stage configuration, so the
    expensive part - "who holds this permission in this scope for this tenant/branch" -
    is memoised on ``(source, permission key, scope, tenant, branch)``.

    ``resolve_approvers`` excludes the requester and then expands delegations *from the
    surviving holders*, so an empty holder set after removing the requester provably
    yields no approvers: a sole approver who is also the requester means parked. That
    lets the memo answer the common case outright and skip the live resolution entirely.
    Organogram stages are requester-relative and cannot be memoised, so they always take
    the live path.
    """

    def __init__(self):
        self._holders: dict = {}

    def _holder_ids(self, stage, instance):
        """Memoised set of base permission holders, or None when not memoisable."""
        if stage.approver_source == ApproverSource.ORGANOGRAM:
            return None
        if not stage.approver_permission_key:
            return frozenset()
        key = (
            stage.approver_source, stage.approver_permission_key,
            stage.approver_scope, instance.tenant_id, instance.branch_id,
        )
        if key not in self._holders:
            # The engine's own public helper, so the scope mapping and its graceful
            # degradation when vs_rbac is absent stay defined in exactly one place.
            # Read-only: nothing in vs_workflow is modified by calling it.
            holders = approvers_service.users_with_permission(
                tenant=instance.tenant, branch=instance.branch,
                permission_key=stage.approver_permission_key,
                scope=ApproverScope(stage.approver_scope),
            )
            self._holders[key] = frozenset(holders.values_list("pk", flat=True))
        return self._holders[key]

    def has_candidates(self, stage, instance) -> bool:
        """Whether resolving this stage live could produce anybody at all.

        Cheap enough to run over a whole page: the underlying holder lookup is issued
        once per stage configuration. False means the stage is still genuinely parked,
        so the caller can skip taking a row lock for it entirely.
        """
        holders = self._holder_ids(stage, instance)
        return holders is None or bool(holders - {instance.requested_by_id})

    def resolve(self, stage, instance) -> list:
        """Return the eligible approvers the engine would resolve for this stage now."""
        if not self.has_candidates(stage, instance):
            return []
        return approvers_service.resolve_approvers(stage, instance)


# --------------------------------------------------------------------------- #
# The repair                                                                  #
# --------------------------------------------------------------------------- #

def _repair_one(stage_instance_id, cache: _ResolutionCache) -> int:
    """Refill one empty approver snapshot under a row lock. Returns rows created."""
    with transaction.atomic():
        # Re-validate every precondition inside the lock: a concurrent vote, skip or
        # terminal transition may have landed between detection and here, and the freeze
        # guarantee (a populated snapshot is never rewritten or added to) is part of it.
        # Two concurrent callers serialise on that lock, so the loser sees the winner's
        # rows and does nothing.
        stage_instance = lock_parked_stage(stage_instance_id)
        if stage_instance is None:
            return 0
        instance = stage_instance.instance

        eligible = cache.resolve(stage_instance.stage, instance)
        if not eligible:
            return 0

        WorkflowStageApprover.objects.bulk_create([
            WorkflowStageApprover(
                stage_instance=stage_instance, user=approver.user,
                on_behalf_of=approver.on_behalf_of, attempt=stage_instance.attempt,
            )
            for approver in eligible
        ])
        # Reuse STAGE_ACTIVATED with an explicit marker, mirroring how routing records
        # its "stage_active_with_no_approvers" warning: the stage did not re-activate,
        # only its eligibility snapshot was filled in.
        audit_service.write(
            instance, AuditEventType.STAGE_ACTIVATED, stage_instance=stage_instance,
            context={
                "repair": "approver_snapshot_refilled",
                "stage_code": stage_instance.stage.code,
                "attempt": stage_instance.attempt,
                "eligible_count": len(eligible),
            },
            message="Approvers became available for a parked stage.",
        )
        return len(eligible)


def repair_stages(stage_instances) -> int:
    """Refill every empty snapshot in ``stage_instances``. Returns rows created.

    Stages that provably still have nobody to resolve are filtered out first, on a
    memoised holder lookup shared across the batch, so the common "the tenant simply has
    no approver yet" case costs one RBAC lookup for the whole page and takes no row
    locks at all. Never advances, approves or skips anything.

    ``stage_instances`` must carry their ``stage`` and ``instance`` (see
    :func:`_empty_active_stages`, which selects both).
    """
    cache = _ResolutionCache()
    return sum(
        _repair_one(row.pk, cache)
        for row in stage_instances
        if cache.has_candidates(row.stage, row.instance)
    )


def repair_workflows(*, tenant=None, instance_id=None) -> int:
    """Repair parked procurement stages in one tenant, or one workflow instance.

    The read paths call this before consulting the frozen snapshots so a newly
    permissioned approver finds the parked work waiting in their inbox without the
    requester having to resubmit.
    """
    qs = _empty_active_stages()
    if tenant is not None:
        qs = qs.filter(instance__tenant=tenant)
    if instance_id is not None:
        qs = qs.filter(instance_id=instance_id)
    return repair_stages(list(qs))


# --------------------------------------------------------------------------- #
# is_parked                                                                    #
# --------------------------------------------------------------------------- #

def _content_type(model):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(model)


def parked_document_ids(documents) -> set:
    """Primary keys of ``documents`` still parked after a repair pass.

    ``documents`` must all be of one procurement model. Documents that are not PENDING
    approval cannot be parked (parking requires an in-flight instance), so they are
    filtered out first and a page with none costs no queries at all. A page that does
    contain pending documents costs one query, and only a page that actually holds a
    parked row pays for the repair and the recheck.
    """
    rows = [d for d in documents if getattr(d, "approval_state", None) == ProcApprovalState.PENDING]
    if not rows:
        return set()
    stages = list(
        _empty_active_stages()
        .filter(
            instance__document_content_type=_content_type(type(rows[0])),
            instance__document_object_id__in=[str(d.pk) for d in rows],
        )
    )
    if not stages:
        return set()
    repair_stages(stages)
    # Recheck: whatever the repair could not staff is genuinely parked.
    still_parked = (
        _empty_active_stages()
        .filter(pk__in=[s.pk for s in stages])
        .values_list("instance__document_object_id", flat=True)
    )
    return {int(object_id) for object_id in still_parked}


def is_document_parked(document) -> bool:
    """Whether one procurement document is parked, repairing it first if it can be."""
    return document.pk in parked_document_ids([document])


def parked_stage_instance(document):
    """The ACTIVE, unstaffed stage instance blocking ``document``, or ``None``.

    Runs the same repair-then-recheck pass as :func:`is_document_parked`, so a document
    that only *looked* parked (somebody has since been granted the permission) yields
    ``None`` here. Callers that intend to act on the stage must still re-assert the
    precondition under a row lock: see :func:`lock_parked_stage`.
    """
    if document.pk not in parked_document_ids([document]):
        return None
    return (
        _empty_active_stages()
        .filter(
            instance__document_content_type=_content_type(type(document)),
            instance__document_object_id=str(document.pk),
        )
        .order_by("-attempt")
        .first()
    )




def parked_document_id_subquery(model):
    """Subquery of parked primary keys for ``model``, for use in a list filter.

    Kept as SQL rather than a materialised id list so the filter stays bounded no
    matter how many documents a badly configured tenant has parked. The
    ``document_object_id`` cast is safe because the content-type filter inside the
    subquery restricts it to this model's own integer primary keys.
    """
    from django.db.models import IntegerField, Subquery
    from django.db.models.functions import Cast

    return Subquery(
        _empty_active_stages()
        .filter(instance__document_content_type=_content_type(model))
        .values(parked_pk=Cast("instance__document_object_id", IntegerField())),
    )
