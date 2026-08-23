"""Parked approvals: detect them, and make them reachable again.

A stage that activates while nobody holds its approving role is **parked**: the
stage is ACTIVE, the instance is IN_PROGRESS, and its frozen approver snapshot
(:class:`~vs_workflow.models.WorkflowStageApprover`) is empty. That is the deliberate
outcome of ``skip_if_no_approvers=False``, which every ladder over money sets: spend
must never approve itself.

Parking on its own would be a trap. This engine resolves eligibility exactly once, at
stage activation (:func:`vs_workflow.services.routing._activate_stage`), and every read
path afterwards - :func:`~vs_workflow.services.actions._check_eligibility`,
``_stage_fully_approved``, :func:`~vs_workflow.services.my_queue.pending_approval_snapshots`
- consults that frozen snapshot rather than live RBAC. A stage activated with **zero**
eligible approvers is therefore permanently unreachable for that attempt: granting
somebody the role afterwards changes nothing, and only a return-to-requester plus
resubmit (a new attempt) would re-snapshot.

This module is the repair for exactly that state, and it lives in the engine because the
defect does. It was first written inside ``vs_procurement`` and fenced to that app's four
document types, which left the same trap open on the other five approvable types the
engine serves - including ``payments.payout_batch``, the path that sends money to a bank.
The logic was never procurement-specific; only the fence was. Callers that still want a
fence pass ``document_types``.

What the repair may do
    Fill an **empty** approver snapshot on an ACTIVE stage by re-resolving approvers with
    the engine's own resolver, under a row lock.

What it must never do
    * Touch a populated snapshot. The freeze guarantee protects an approver who is
      mid-review from having their eligibility rewritten under them; the emptiness check
      is re-run *inside* the lock and is the hard precondition for any write.
    * Approve, advance, skip, or otherwise move a workflow. It restores reachability and
      nothing else - the decision still needs a human.

Because the repair only ever fires on a snapshot of size zero, it cannot change a stage's
advance arithmetic retroactively: an ``ANY`` stage with no approvers had no possible vote,
and a ``UNANIMOUS`` stage requires ``eligible_count > 0`` before it can advance at all.
"""
from __future__ import annotations

import logging

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

logger = logging.getLogger("vs_workflow.parking")


# --------------------------------------------------------------------------- #
# Detection - a pure database predicate                                       #
# --------------------------------------------------------------------------- #

def empty_active_stages(document_types=None):
    """ACTIVE stages whose approver snapshot is empty for this attempt.

    One indexed query, no RBAC involvement: on healthy data it matches nothing and every
    caller short-circuits before resolving a single role holder.

    ``document_types`` narrows the scan to a set of workflow document-type tokens; the
    default is every type the engine serves, which is what makes this a repair for the
    engine rather than for one app.
    """
    qs = WorkflowStageInstance.objects.filter(
        status=WorkflowStageStatus.ACTIVE,
        instance__status=WorkflowInstanceStatus.IN_PROGRESS,
    )
    if document_types is not None:
        qs = qs.filter(instance__document_type__in=document_types)
    return qs.filter(
        ~Exists(WorkflowStageApprover.objects.filter(
            stage_instance=OuterRef("pk"), attempt=OuterRef("attempt"),
        )),
    ).select_related(
        "stage", "instance",
        # Everything the resolution cache touches per row, fetched with the scan
        # rather than lazily. A page of parked documents walks each of these once
        # per row, so a missing relation here is a descriptor query PER STAGE,
        # which is exactly the per-row cost this module's cache and its
        # query-count test exist to prevent. ``instance__tenant`` is the one that
        # bites hardest: both the override lookup and the role-holder lookup take
        # the tenant object, not its id.
        "instance__tenant", "instance__branch",
        # Read by ``stage_role_key``'s fallback and by ``stage_requirement``.
        "stage__approver_role", "stage__approver_group",
    )


def lock_parked_stage(stage_instance_id, document_types=None):
    """Lock one stage instance and return it only if it is *still* genuinely parked.

    The single in-transaction definition of "parked", shared by the repair below and by
    any caller that wants to act on a parked stage (procurement's approval override), so
    the two can never disagree about what they are allowed to touch. Returns ``None``
    when any precondition has moved underneath the caller - a repair staffed the stage, a
    vote landed, the instance went terminal - which the caller must treat as a refusal,
    not a retry.

    Must be called inside an open transaction; the lock is held until it commits.
    """
    stage_instance = (
        # ``of="self"`` locks only the stage-instance row: the joined instance and
        # branch are nullable sides of an outer join, which Postgres refuses to lock.
        WorkflowStageInstance.objects.select_for_update(of=("self",))
        .select_related(
            "stage", "instance", "instance__tenant", "instance__branch",
            "stage__approver_role", "stage__approver_group",
        )
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
    if document_types is not None and instance.document_type not in document_types:
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

class ResolutionCache:
    """Resolve stage approvers, sharing the RBAC holder lookup across stages.

    Several parked documents on one page usually share a stage configuration, so the
    expensive part - "who holds this role in this scope for this tenant/branch" -
    is memoised on ``(source, role key, scope, tenant, branch)``.

    ``resolve_approvers`` excludes the requester and then expands delegations *from the
    surviving holders*, so an empty holder set after removing the requester provably
    yields no approvers: a sole approver who is also the requester means parked. That
    lets the memo answer the common case outright and skip the live resolution entirely.

    The memo is **opt-in per source, not opt-out**, and that direction matters. Only
    ROLE stages carrying a key can be answered from a role-holder lookup; every other
    source - approver groups, document-driven rules, the organogram - falls through to
    the live path. The alternative default, treating an unrecognised
    source as "provably nobody", would make the repair silently skip those stages, and a
    stage the repair skips is a document that parks and never un-parks. That is the exact
    failure this module exists to prevent, so an unknown source must cost a query rather
    than a lost document.
    """

    def __init__(self):
        self._holders: dict = {}
        self._overrides: dict = {}

    def _override_for(self, stage, tenant):
        """Memoised tenant override for this stage, or None.

        Memoised for the same reason the holder lookup is: a page of parked
        documents usually shares one stage, and an unmemoised lookup here costs a
        query per row, which is precisely the per-stage cost this cache exists to
        remove. Keyed on (stage, tenant) because that is what an override is
        scoped to.
        """
        key = (stage.pk, getattr(tenant, "pk", None))
        if key not in self._overrides:
            self._overrides[key] = approvers_service.stage_override_for(stage, tenant)
        return self._overrides[key]

    def _holder_ids(self, stage, instance):
        """Memoised set of base role holders, or None when not memoisable.

        ``None`` means "cannot answer from the memo, resolve it live", which is the
        safe answer for every source this function does not explicitly understand.
        """
        if stage.approver_source != ApproverSource.ROLE:
            return None
        role_key = approvers_service.stage_role_key(stage)
        if not role_key:
            # A role-sourced stage with no key is misconfigured rather than
            # unstaffable. Resolve it live so the engine's own resolver decides,
            # instead of concluding here that nobody can ever approve it.
            return None
        # A tenant may have repointed this stage at its own role or group, in
        # which case the stage's own key is not what will resolve. Fall through.
        if self._override_for(stage, instance.tenant) is not None:
            return None
        branch = (instance.branch
                  if stage.approver_scope == ApproverScope.BRANCH else None)
        key = (
            stage.approver_source, role_key,
            stage.approver_scope, instance.tenant_id, instance.branch_id,
        )
        if key not in self._holders:
            # The engine's own public helper, so the scope mapping stays defined
            # in exactly one place.
            self._holders[key] = approvers_service.role_holder_ids(
                role_key=role_key, tenant=instance.tenant, branch=branch,
            )
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

def _repair_one(stage_instance_id, cache: ResolutionCache, document_types=None) -> int:
    """Refill one empty approver snapshot under a row lock. Returns rows created."""
    with transaction.atomic():
        # Re-validate every precondition inside the lock: a concurrent vote, skip or
        # terminal transition may have landed between detection and here, and the freeze
        # guarantee (a populated snapshot is never rewritten or added to) is part of it.
        # Two concurrent callers serialise on that lock, so the loser sees the winner's
        # rows and does nothing.
        stage_instance = lock_parked_stage(stage_instance_id, document_types)
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
        # Tell the people who just became eligible. Until this existed a repaired
        # document waited silently: the stage was already ACTIVE, so no activation
        # notification had ever fired for it, and the audit row nobody reads was the
        # only trace. Whoever was appointed to the role discovered the waiting work
        # only by opening the queue on spec, which can be days after it became
        # approvable. Same event and recipients as a stage activating normally,
        # because that is exactly what it means to them; the audit entry above
        # carries the marker distinguishing a repair from a real activation.
        # Imported here rather than at module load: routing imports this module's
        # siblings and the notifier is only needed on the rare repairing pass.
        from vs_workflow.constants import NOTIF_EVENT_STAGE_ACTIVATED
        from vs_workflow.services.routing import notify

        notify(
            instance, NOTIF_EVENT_STAGE_ACTIVATED,
            recipient_user_ids=list({str(approver.user.id) for approver in eligible}),
            context={
                "stage_name": stage_instance.stage.label,
                "stage_label": stage_instance.stage.label,
                "submitter_name": instance.requested_by.full_name,
            },
        )
        return len(eligible)


def repair_stages(stage_instances, document_types=None) -> int:
    """Refill every empty snapshot in ``stage_instances``. Returns rows created.

    Stages that provably still have nobody to resolve are filtered out first, on a
    memoised holder lookup shared across the batch, so the common "the tenant simply has
    no approver yet" case costs one RBAC lookup for the whole page and takes no row locks
    at all. Never advances, approves or skips anything.

    ``stage_instances`` must carry their ``stage`` and ``instance`` (see
    :func:`empty_active_stages`, which selects both).
    """
    cache = ResolutionCache()
    repaired = 0
    for row in stage_instances:
        # One unrepairable stage must not abort the pass. This runs on the read
        # path, over whatever happens to be parked in the tenant, so a single
        # broken template (a stage naming an approver source the resolver cannot
        # resolve, which raises by design) would otherwise take down the whole
        # approvals inbox for everybody in that tenant. The resolver is right to
        # raise; this sweep is best-effort and degrades to leaving the stage
        # parked, which is the state it was already in.
        try:
            if not cache.has_candidates(row.stage, row.instance):
                continue
            repaired += _repair_one(row.pk, cache, document_types)
        except Exception:  # noqa: BLE001 - see above
            logger.exception(
                "parking: could not repair stage instance %s (stage %s, source %s).",
                row.pk, row.stage.code, row.stage.approver_source,
            )
    return repaired


def repair_workflows(*, tenant=None, instance_id=None, document_types=None) -> int:
    """Repair parked stages in one tenant, or one workflow instance.

    The read paths call this before consulting the frozen snapshots so a newly
    appointed approver finds the parked work waiting in their inbox without the
    requester having to resubmit.
    """
    qs = empty_active_stages(document_types)
    if tenant is not None:
        qs = qs.filter(instance__tenant=tenant)
    if instance_id is not None:
        qs = qs.filter(instance_id=instance_id)
    return repair_stages(list(qs), document_types)


# --------------------------------------------------------------------------- #
# is_parked                                                                    #
# --------------------------------------------------------------------------- #

def _content_type(model):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(model)


def parked_object_ids(model, pks, document_types=None) -> set:
    """Primary keys among ``pks`` of ``model`` still parked after a repair pass.

    Callers pre-filter to documents that could plausibly be parked (parking requires an
    in-flight instance), so an empty ``pks`` costs no queries at all. A page that does
    contain candidates costs one query, and only a page that actually holds a parked row
    pays for the repair and the recheck.
    """
    pks = list(pks)
    if not pks:
        return set()
    stages = list(
        empty_active_stages(document_types)
        .filter(
            instance__document_content_type=_content_type(model),
            instance__document_object_id__in=[str(pk) for pk in pks],
        )
    )
    if not stages:
        return set()
    repair_stages(stages, document_types)
    # Recheck: whatever the repair could not staff is genuinely parked.
    still_parked = (
        empty_active_stages(document_types)
        .filter(pk__in=[s.pk for s in stages])
        .values_list("instance__document_object_id", flat=True)
    )
    return {int(object_id) for object_id in still_parked}


def parked_stage_instance(model, pk, document_types=None):
    """The ACTIVE, unstaffed stage instance blocking one document, or ``None``.

    Runs the same repair-then-recheck pass as :func:`parked_object_ids`, so a document
    that only *looked* parked (somebody has since been appointed to the role) yields
    ``None`` here. Callers that intend to act on the stage must still re-assert the
    precondition under a row lock: see :func:`lock_parked_stage`.
    """
    if pk not in parked_object_ids(model, [pk], document_types):
        return None
    return (
        empty_active_stages(document_types)
        .filter(
            instance__document_content_type=_content_type(model),
            instance__document_object_id=str(pk),
        )
        .order_by("-attempt")
        .first()
    )


def parked_id_subquery(model, document_types=None):
    """Subquery of parked primary keys for ``model``, for use in a list filter.

    Kept as SQL rather than a materialised id list so the filter stays bounded no matter
    how many documents a badly configured tenant has parked. The ``document_object_id``
    cast is safe because the content-type filter inside the subquery restricts it to this
    model's own integer primary keys.
    """
    from django.db.models import IntegerField, Subquery
    from django.db.models.functions import Cast

    return Subquery(
        empty_active_stages(document_types)
        .filter(instance__document_content_type=_content_type(model))
        .values(parked_pk=Cast("instance__document_object_id", IntegerField())),
    )
