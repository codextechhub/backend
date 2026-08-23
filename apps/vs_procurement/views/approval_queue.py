"""Entity-safe Procurement inbox adapters over the shared workflow engine.

The workflow tables are tenant-scoped and point to business records through a
generic foreign key; a ledger entity is a narrower boundary than a tenant. These
views therefore resolve the selected entity and verify the real Procurement target
before returning or mutating an instance. Decision routing, locks, eligibility,
thresholds and terminal callbacks remain wholly owned by ``vs_workflow``.
"""
from __future__ import annotations

from django.db.models import F
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.views import resolve_entity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_tenants.models import Branch
from vs_workflow.models import (
    WorkflowStageAction,
    WorkflowStageApprover,
    WorkflowInstance,
)
from vs_workflow.serializers import StageActionWriteSerializer
from vs_workflow.services import actions as workflow_actions
from vs_workflow.services.routing import preview_next_approval_stage

from .base import _ProcBase, _branch_q, _caller_branch_ids
from .. import approval_coverage, approval_override, approval_parking
from ..constants import (
    PROCUREMENT_APPROVAL_TYPES,
    WF_APPROVAL_OVERRIDE_PERMISSION,
    WF_DOCTYPE_PURCHASE_ORDER,
    WF_DOCTYPE_REQUISITION,
    WF_DOCTYPE_VENDOR_INVOICE,
    WF_DOCTYPE_VENDOR_PAYMENT,
)
from ..models import PurchaseOrder, PurchaseRequisition, VendorInvoice, VendorPayment


DOCUMENT_MODELS = {
    WF_DOCTYPE_REQUISITION: PurchaseRequisition,
    WF_DOCTYPE_PURCHASE_ORDER: PurchaseOrder,
    WF_DOCTYPE_VENDOR_INVOICE: VendorInvoice,
    WF_DOCTYPE_VENDOR_PAYMENT: VendorPayment,
}

DOCUMENT_LABELS = {
    WF_DOCTYPE_REQUISITION: "Requisition",
    WF_DOCTYPE_PURCHASE_ORDER: "Purchase Order",
    WF_DOCTYPE_VENDOR_INVOICE: "Vendor Invoice",
    WF_DOCTYPE_VENDOR_PAYMENT: "Vendor Payment",
}


def _user_name(user) -> str:
    """Render an actor label without exposing email or other account metadata."""
    if user is None:
        return "System"
    return (
        getattr(user, "full_name", "")
        or user.get_full_name()
        or "Unknown user"
    )


def _document_title(document, document_type: str) -> str:
    """Choose a persisted display label without reading workflow JSON metadata."""
    if document_type == WF_DOCTYPE_REQUISITION:
        return document.title or document.justification or "Purchase requisition"
    vendor = getattr(document, "vendor", None)
    vendor_name = getattr(vendor, "name", "")
    if document_type == WF_DOCTYPE_PURCHASE_ORDER:
        return document.narration or document.reference or vendor_name or "Purchase order"
    if document_type == WF_DOCTYPE_VENDOR_INVOICE:
        return document.narration or document.vendor_reference or vendor_name or "Vendor invoice"
    return document.narration or document.reference or (
        f"{vendor_name} payment" if vendor_name else "Vendor payment"
    )


def _document_map(entity, snapshots, branch_filter):
    """Bulk-resolve generic workflow targets inside the caller's entity and branch.

    ``branch_filter`` narrows the resolution exactly as the document lists do (see
    ``views.base._branch_q``), so an approver bound to one site can never read or act
    on another site's document through the queue - an unresolved target is skipped by
    every caller here, which is the same "no such approval" answer as a foreign entity.
    """
    ids_by_type = {document_type: set() for document_type in PROCUREMENT_APPROVAL_TYPES}
    usable = []
    seen = set()
    for snapshot in snapshots:
        instance = snapshot.stage_instance.instance
        if instance.id in seen:
            continue
        try:
            object_id = int(instance.document_object_id)
        except (TypeError, ValueError):
            continue
        ids_by_type[instance.document_type].add(object_id)
        usable.append((snapshot, object_id))
        seen.add(instance.id)

    documents = {}
    for document_type, model in DOCUMENT_MODELS.items():
        qs = model.objects.filter(
            entity=entity, pk__in=ids_by_type[document_type],
        ).filter(branch_filter)
        if document_type != WF_DOCTYPE_REQUISITION:
            qs = qs.select_related("vendor")
        documents[document_type] = {row.pk: row for row in qs}
    return usable, documents


def _pending_snapshots(user, *, workflow_id=None):
    """Return current frozen approver snapshots, excluding votes already cast."""
    qs = (
        WorkflowStageApprover.objects.filter(
            user=user,
            attempt=F("stage_instance__attempt"),
            stage_instance__status="ACTIVE",
            stage_instance__instance__status="IN_PROGRESS",
            stage_instance__instance__document_type__in=PROCUREMENT_APPROVAL_TYPES,
        )
        .select_related(
            "on_behalf_of",
            "stage_instance__stage",
            "stage_instance__instance__requested_by",
            "stage_instance__instance__current_stage",
        )
        .order_by("-stage_instance__activated_at")
    )
    if workflow_id is not None:
        # Detail/action reads narrow before materialising frozen snapshots.
        qs = qs.filter(stage_instance__instance_id=workflow_id)
    snapshots = list(qs)
    stage_ids = {snapshot.stage_instance_id for snapshot in snapshots}
    acted = set(
        WorkflowStageAction.objects.filter(
            actor=user,
            stage_instance_id__in=stage_ids,
            reversed_at__isnull=True,
            is_reversal_of__isnull=True,
        ).values_list("stage_instance_id", "attempt")
    ) if stage_ids else set()
    return [
        snapshot for snapshot in snapshots
        if (snapshot.stage_instance_id, snapshot.stage_instance.attempt) not in acted
    ]


def _list_row(entity, snapshot, document, object_id):
    """Project one workflow snapshot into the procurement inbox response shape."""
    instance = snapshot.stage_instance.instance
    amount_field = getattr(document, "workflow_amount_field", "")
    return {
        "id": instance.id,
        "document_type": instance.document_type,
        "document_type_label": DOCUMENT_LABELS[instance.document_type],
        "document_id": object_id,
        "reference": document.document_number or str(document.pk),
        "title": _document_title(document, instance.document_type),
        "requester": _user_name(instance.requested_by),
        "amount": int(getattr(document, amount_field, 0) or 0),
        "currency": entity.base_currency_id,
        "submitted_at": instance.submitted_at,
        "awaiting_since": snapshot.stage_instance.activated_at,
        "stage": snapshot.stage_instance.stage.label,
        "status": instance.status,
        "on_behalf_of": _user_name(snapshot.on_behalf_of) if snapshot.on_behalf_of_id else None,
    }


def _pending_context(entity, user, workflow_id, branch_filter):
    """Resolve an actionable workflow and its real document without existence leaks."""
    snapshots = _pending_snapshots(user, workflow_id=workflow_id)
    usable, documents = _document_map(entity, snapshots, branch_filter)
    if not usable:
        # One indistinguishable 404 covers foreign entities, document families,
        # ineligible users, completed votes and stale workflow attempts.
        raise NotFound("No pending Procurement approval with this id exists in the selected entity.")
    snapshot, object_id = usable[0]
    instance = snapshot.stage_instance.instance
    document = documents[instance.document_type].get(object_id)
    if document is None:
        raise NotFound("No pending Procurement approval with this id exists in the selected entity.")
    return instance, snapshot, document, object_id


def _stage_rows(instance):
    """Serialize prefetched stage evidence without per-stage database queries."""
    rows = []
    for stage_instance in instance.stage_instances.all():
        actions = []
        for action in stage_instance.actions.all():
            actions.append({
                "id": str(action.id),
                "action": action.action,
                "actor": _user_name(action.actor),
                "on_behalf_of": _user_name(action.on_behalf_of) if action.on_behalf_of_id else None,
                "comment": action.comment,
                "acted_at": action.acted_at,
                "attempt": action.attempt,
                "is_reversal": action.is_reversal_of_id is not None,
                "reversed_at": action.reversed_at,
            })
        rows.append({
            "id": str(stage_instance.id),
            "label": stage_instance.stage.label,
            "status": stage_instance.status,
            "on_rejection": stage_instance.stage.on_rejection,
            "advance_rule": stage_instance.stage.advance_rule,
            "quorum_count": stage_instance.stage.quorum_count,
            # Count the prefetched snapshot in memory; filtering the related manager
            # here would issue one extra query for every stage in the drawer.
            "eligible_count": sum(
                approver.attempt == stage_instance.attempt
                for approver in stage_instance.eligible_approvers.all()
            ),
            "activated_at": stage_instance.activated_at,
            "resolved_at": stage_instance.resolved_at,
            "skip_reason": stage_instance.skip_reason,
            "attempt": stage_instance.attempt,
            "actions": actions,
        })
    return rows


def _detail(entity, instance, snapshot, document, object_id):
    """Overlay workflow history on the entity-verified procurement summary row."""
    data = _list_row(entity, snapshot, document, object_id)
    data.update({
        "document_status": getattr(document, "status", ""),
        "approval_state": getattr(document, "approval_state", ""),
        "next_stage": preview_next_approval_stage(instance),
        "stages": _stage_rows(instance),
        "activity": [{
            "id": str(log.id),
            "event_type": log.event_type,
            "actor": _user_name(log.actor) if log.actor_id else None,
            "message": log.message,
            "occurred_at": log.occurred_at,
        } for log in instance.audit_logs.all()],
    })
    return data


def _entity_workflow_document(entity, workflow_id, branch_filter):
    """Resolve one procurement workflow and its document inside ``entity``.

    Deliberately *not* the eligibility lookup :func:`_pending_context` performs: a parked
    stage has no eligible approver by definition, so the actor releasing it is never in
    the frozen snapshot. Isolation is therefore carried by the entity and the caller's
    branch: the instance must belong to the entity's tenant, be one of Procurement's own
    document families, and resolve to a document in *this* entity's books that the
    caller may work in. A foreign tenant, a foreign entity, another branch's document, a
    finance or payout workflow and a genuinely unknown id all return the same 404, so
    possession of a workflow id reveals nothing.

    The branch term matters here as much as anywhere else: an override releases spend
    without review, and a caller who may not read a document must not be able to
    release it either.
    """
    instance = (
        WorkflowInstance.all_objects
        .filter(
            pk=workflow_id, tenant=entity.tenant,
            document_type__in=PROCUREMENT_APPROVAL_TYPES,
        )
        .select_related("current_stage")
        .first()
    )
    missing = NotFound("No Procurement approval with this id exists in the selected entity.")
    if instance is None:
        raise missing
    try:
        object_id = int(instance.document_object_id)
    except (TypeError, ValueError):
        raise missing
    document = DOCUMENT_MODELS[instance.document_type].objects.filter(
        entity=entity, pk=object_id,
    ).filter(branch_filter).first()
    if document is None:
        raise missing
    return instance, document


class ProcurementApprovalCoverageView(_ProcBase):
    """Who can approve procurement spend here, and where nobody can.

    The evidence behind the approval-rules screen: for every branch (plus the
    entity-level scope) it reports the ladder that would actually run there and the
    people who currently hold each approving permission - and, where a stage has
    nobody behind it, says so. A stage with no approver is not a cosmetic problem: any
    document reaching it parks until an administrator appoints someone.

    Read-only. It resolves rules and people through the workflow engine's own template
    cascade and eligibility lookup, so it cannot disagree with live routing. A caller
    bound to a branch sees that branch's scope only, the same narrowing every other
    procurement read is under.

    Only display names are returned, never account identifiers or email addresses.

    docstring-name: Approval coverage
    """
    rbac_permission = "procurement.approval.manage"

    def get(self, request):
        """Report approver coverage per branch for the caller's own tenant."""
        entity = resolve_entity(request)
        ids = _caller_branch_ids(request)
        return success_response("Approval coverage retrieved.", data=approval_coverage.approval_coverage(
            entity.tenant,
            branches=None if ids is None else list(
                Branch.all_objects.filter(tenant=entity.tenant, pk__in=ids).order_by("code"),
            ),
            # A caller who only works in certain branches is reported those alone;
            # the entity-level scope is somebody else's to staff.
            include_entity_level=ids is None,
        ))


class ProcurementApprovalOverrideView(APIView):
    """Release a **parked** approval without a vote, on a written reason.

    The escape hatch for a document nobody is able to decide (see
    :mod:`vs_procurement.approval_parking`). Gated on ``procurement.approval.override``,
    a CRITICAL key granted to no role by default; the service re-checks it against the
    document's own tenant and refuses anything that is not actually parked, so this view
    cannot widen the capability. Every release writes an immutable override record, a
    workflow audit entry naming the actor, and a finance audit row.

    POST body: ``reason`` (required, free text).

    docstring-name: Override a parked approval
    """
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = WF_APPROVAL_OVERRIDE_PERMISSION

    def post(self, request, workflow_id):
        """Release the entity-verified parked stage and report where the document landed."""
        entity = resolve_entity(request)
        instance, document = _entity_workflow_document(
            entity, workflow_id, _branch_q(request, entity),
        )
        override = approval_override.release_parked_document(
            document, actor_user=request.user,
            reason=request.data.get("reason"),
        )
        instance.refresh_from_db()
        return success_response("Parked approval released by override.", data={
            "id": instance.id,
            "workflow_status": instance.status,
            "current_stage_label": getattr(instance.current_stage, "label", None),
            "document_type": instance.document_type,
            "document_id": document.pk,
            "approval_state": getattr(document, "approval_state", ""),
            "document_status": getattr(document, "status", ""),
            "override": {
                "id": override.pk,
                "stage_code": override.stage_code,
                "amount": override.amount,
                "reason": override.reason,
                "overridden_by": _user_name(request.user),
                "overridden_at": override.created_at,
            },
        })


class ProcurementApprovalListView(APIView):
    """The signed-in actor's pending Procurement decisions for one ledger entity.

    Ordinary resource RBAC is intentionally not the authorization rule here.
    Being frozen into the active workflow stage is the narrower, instance-level
    eligibility grant; users who cannot currently vote see no row at all.
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request):
        """Return entity-verified, currently actionable snapshots for this actor."""
        entity = resolve_entity(request)
        # A stage that activated while nobody held its permission has an empty frozen
        # snapshot and would never appear here, however the permissions changed since.
        # Restore reachability first, then read; one indexed query when nothing is parked.
        approval_parking.repair_workflows(tenant=entity.tenant)
        usable, documents = _document_map(
            entity, _pending_snapshots(request.user), _branch_q(request, entity),
        )
        rows = []
        for snapshot, object_id in usable:
            instance = snapshot.stage_instance.instance
            document = documents[instance.document_type].get(object_id)
            if document is not None:
                rows.append(_list_row(entity, snapshot, document, object_id))

        document_type = request.query_params.get("document_type", "").strip()
        if document_type:
            rows = [row for row in rows if row["document_type"] == document_type]
        search = request.query_params.get("search", "").strip().lower()
        if search:
            rows = [row for row in rows if search in " ".join((
                row["reference"], row["title"], row["requester"], row["document_type_label"],
            )).lower()]

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(page)


class ProcurementApprovalDetailView(APIView):
    """Safe workflow evidence for one currently actionable Procurement record.

    The same eligibility lookup as the list is repeated here; possession of a
    workflow UUID is never enough to read another approver's decision evidence.
    """
    permission_classes = [IsAuthenticatedAndActive]

    def get(self, request, workflow_id):
        """Return the prefetched stage timeline for an eligible entity document."""
        entity = resolve_entity(request)
        approval_parking.repair_workflows(tenant=entity.tenant, instance_id=workflow_id)
        instance, snapshot, document, object_id = _pending_context(
            entity, request.user, workflow_id, _branch_q(request, entity),
        )
        instance = (
            WorkflowInstance.all_objects.filter(pk=instance.pk)
            .select_related("requested_by", "template", "current_stage")
            .prefetch_related(
                "stage_instances__stage",
                "stage_instances__eligible_approvers",
                "stage_instances__actions__actor",
                "stage_instances__actions__on_behalf_of",
                "audit_logs__actor",
            )
            .get()
        )
        return success_response(
            "Procurement approval retrieved.",
            data=_detail(entity, instance, snapshot, document, object_id),
        )


class ProcurementApprovalActionView(APIView):
    """Validate entity/document scope, then record the vote in ``vs_workflow``.

    Workflow eligibility replaces a broad approve-resource permission: the
    shared workflow service rechecks active attempt, quorum, and transition
    rules under its own locks when it records the decision.
    """
    permission_classes = [IsAuthenticatedAndActive]

    def post(self, request, workflow_id):
        """Validate a decision payload and delegate the state transition atomically."""
        entity = resolve_entity(request)
        # Eligibility is read from the frozen snapshot, so repair a parked stage before
        # the check rather than after it; the repair itself never records a decision.
        approval_parking.repair_workflows(tenant=entity.tenant, instance_id=workflow_id)
        instance, _, _, _ = _pending_context(
            entity, request.user, workflow_id, _branch_q(request, entity),
        )
        serializer = StageActionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data
        updated = workflow_actions.record_action(
            instance.id,
            request.user,
            action=decision["action"],
            comment=decision.get("comment", ""),
        )
        return success_response("Approval decision recorded.", data={
            "id": updated.id,
            "status": updated.status,
            "current_stage_label": getattr(updated.current_stage, "label", None),
        })
