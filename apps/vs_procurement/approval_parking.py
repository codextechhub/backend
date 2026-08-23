"""Parked spend approvals, scoped to procurement.

The repair itself now lives in the engine, at
:mod:`vs_workflow.services.parking`, because the defect it fixes is the engine's:
``vs_workflow`` freezes a stage's approver snapshot at activation, so a stage that
activated with nobody eligible stays unreachable for that attempt even after somebody is
granted the permission. That is true of every approvable document type, not just the four
here. This module was where the repair was first written and fenced; the fence is all
that remains of it.

What is left is deliberately procurement-shaped and worth keeping local:

* every entry point passes :data:`~vs_procurement.constants.PROCUREMENT_APPROVAL_TYPES`,
  so procurement's callers - the approval queue, the serializers, the override in
  :mod:`vs_procurement.approval_override` - keep operating strictly on procurement work
  even though the underlying repair no longer has to;
* :func:`parked_document_ids` pre-filters on ``approval_state == PENDING``, which is a
  procurement overlay the engine knows nothing about, and is what makes a page with no
  pending documents cost zero queries.

The guarantees are unchanged and are documented on the engine module: a populated
snapshot is never touched, nothing is ever approved, advanced or skipped, and the
emptiness precondition is re-asserted inside the row lock.
"""
from __future__ import annotations

from vs_workflow.services import parking as engine_parking

from .constants import PROCUREMENT_APPROVAL_TYPES, ProcApprovalState


def _empty_active_stages():
    """ACTIVE procurement stages whose approver snapshot is empty for this attempt."""
    return engine_parking.empty_active_stages(PROCUREMENT_APPROVAL_TYPES)


def lock_parked_stage(stage_instance_id):
    """Lock one procurement stage instance, returning it only if still parked.

    Shared with :mod:`vs_procurement.approval_override` so the repair and the override
    can never disagree about what counts as parked. Must be called inside an open
    transaction.
    """
    return engine_parking.lock_parked_stage(stage_instance_id, PROCUREMENT_APPROVAL_TYPES)


def repair_stages(stage_instances) -> int:
    """Refill every empty snapshot in ``stage_instances``. Returns rows created."""
    return engine_parking.repair_stages(stage_instances, PROCUREMENT_APPROVAL_TYPES)


def repair_workflows(*, tenant=None, instance_id=None) -> int:
    """Repair parked procurement stages in one tenant, or one workflow instance.

    The read paths call this before consulting the frozen snapshots so a newly
    permissioned approver finds the parked work waiting in their inbox without the
    requester having to resubmit.
    """
    return engine_parking.repair_workflows(
        tenant=tenant, instance_id=instance_id,
        document_types=PROCUREMENT_APPROVAL_TYPES,
    )


def parked_document_ids(documents) -> set:
    """Primary keys of ``documents`` still parked after a repair pass.

    ``documents`` must all be of one procurement model. Documents that are not PENDING
    approval cannot be parked (parking requires an in-flight instance), so they are
    filtered out here - the engine has no idea what ``approval_state`` is - and a page
    with none costs no queries at all.
    """
    rows = [d for d in documents if getattr(d, "approval_state", None) == ProcApprovalState.PENDING]
    if not rows:
        return set()
    return engine_parking.parked_object_ids(
        type(rows[0]), [d.pk for d in rows], PROCUREMENT_APPROVAL_TYPES,
    )


def is_document_parked(document) -> bool:
    """Whether one procurement document is parked, repairing it first if it can be."""
    return document.pk in parked_document_ids([document])


def parked_stage_instance(document):
    """The ACTIVE, unstaffed stage instance blocking ``document``, or ``None``.

    Runs the same repair-then-recheck pass as :func:`is_document_parked`, so a document
    that only *looked* parked yields ``None``. Callers that intend to act on the stage
    must still re-assert the precondition under a row lock: see
    :func:`lock_parked_stage`.
    """
    if getattr(document, "approval_state", None) != ProcApprovalState.PENDING:
        return None
    return engine_parking.parked_stage_instance(
        type(document), document.pk, PROCUREMENT_APPROVAL_TYPES,
    )


def parked_document_id_subquery(model):
    """Subquery of parked primary keys for ``model``, for use in a list filter."""
    return engine_parking.parked_id_subquery(model, PROCUREMENT_APPROVAL_TYPES)
