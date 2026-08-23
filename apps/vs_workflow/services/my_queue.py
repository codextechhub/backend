"""A person's own approval queue - the snapshots they may still act on.

Extracted from ``PendingApprovalsView`` so the dashboard aggregate counts the
queue with the *same* rules the queue screen lists it by. Two implementations of
"pending for me" would drift the moment either changed, and the number a user
sees on their landing screen would stop matching the rows they find when they
click through.

The three filters that make a snapshot actionable are all here:
  * live work only - an ACTIVE stage on an IN_PROGRESS instance;
  * not already voted - the actor has an unreversed action for this attempt;
  * not stale - snapshots from a previous attempt of the same stage are ignored,
    which happens when a returned instance is resubmitted.
"""

from __future__ import annotations

from vs_workflow.models import WorkflowStageAction, WorkflowStageApprover
from vs_workflow.services import parking


def pending_approval_snapshots(user, tenant=None) -> list[WorkflowStageApprover]:
    """Approver snapshots *user* can still act on, newest activation first.

    Starts from snapshots rather than instances so delegated approvals - where
    the actor is not the original approver - are included.

    Parked work is restored before reading. A stage that activated while nobody
    could approve it has an empty frozen snapshot, so it would never appear here
    however the roles changed afterwards; see :mod:`vs_workflow.services.parking`.
    Repairing at this choke point rather than in one screen is what gives every
    document type the same treatment - the alternative is each app remembering to
    do it, which is how payout batches went without it. On healthy data it is one
    indexed query that matches nothing and takes no locks.
    """
    # Scope the repair to the books being read, never system-wide: one person
    # opening their queue must not do work on behalf of other tenants.
    repair_tenant = tenant if tenant is not None else getattr(user, "tenant", None)
    if repair_tenant is not None:
        parking.repair_workflows(tenant=repair_tenant)

    snaps_qs = WorkflowStageApprover.objects.filter(
        user=user,
        stage_instance__status="ACTIVE",
        stage_instance__instance__status="IN_PROGRESS",
    )
    if tenant is not None:
        snaps_qs = snaps_qs.filter(stage_instance__instance__tenant=tenant)

    snaps = snaps_qs.select_related(
        "stage_instance__instance__template", "stage_instance__stage",
    ).order_by("-stage_instance__activated_at")

    already_acted = set(
        WorkflowStageAction.objects.filter(
            actor=user, reversed_at__isnull=True, is_reversal_of__isnull=True,
        ).values_list("stage_instance_id", "attempt")
    )

    return [
        snap for snap in snaps
        if (snap.stage_instance_id, snap.stage_instance.attempt) not in already_acted
        and snap.attempt == snap.stage_instance.attempt
    ]


def pending_approval_count(user, tenant=None) -> int:
    """How many decisions are waiting on *user*.

    Deliberately materialises the same list as the screen instead of issuing a
    cheaper ``COUNT(*)``: the already-acted and stale-attempt filters cannot be
    expressed in the snapshot query, so a count that skipped them would report
    work the user cannot actually action.
    """
    return len(pending_approval_snapshots(user, tenant))
