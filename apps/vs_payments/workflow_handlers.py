"""vs_workflow handler for approval-gating bulk payout batches.

A :class:`~vs_payments.models.PayoutBatch` disburses money *out* to one or more
beneficiaries, so it is a high-risk cash-out path. Every payout enters this handler,
including a single payout represented as a one-line batch. Provider submission happens
only from terminal approval; a missing template fails closed before this callback.

Unlike a finance document, a payout batch is **not** a GL posting - approval gates
the *provider submission* (:func:`vs_payments.services.submit_payout_batch`), which
dispatches the batch's pending instructions to the PSP. The batch's own
``PayoutBatchStatus`` has no approval states, so we track the approval phase in
``metadata["approval_status"]`` and leave the batch ``DRAFT`` until it is approved
and submitted (at which point the service moves it to ``PROCESSING``).

Approval and dispatch are deliberately **not** the same transaction: see
:meth:`PayoutBatchApprovalHandler.on_approved`. Approval commits, and only then is the
provider called - from a worker, off ``transaction.on_commit``.

Auto-discovered by the engine on startup via ``autodiscover_modules("workflow_handlers")``.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.db import transaction

from vs_workflow.constants import WorkflowStageAction as StageActionEnum
from vs_workflow.exceptions import InvalidInstanceStateError
from vs_workflow.handlers import BaseWorkflowHandler, register_handler

logger = logging.getLogger("vs_payments.workflow_handlers")  # Diagnostics for the dispatch hand-off.


# Support the post-approval dispatch hand-off.
def _enqueue_dispatch(batch_id, instance_id, actor_id) -> None:
    """Hand an approved batch to a worker (local import avoids a task-discovery cycle)."""
    from .tasks import dispatch_payout_batch

    try:
        dispatch_payout_batch.delay(batch_id, instance_id, actor_id)  # Inline under ALWAYS_EAGER.
    except Exception:  # noqa: BLE001 - the approval is already committed; do not lose the batch.
        # Never let a broker problem propagate out of an on_commit hook: the approval is
        # durable, the batch is findable, and the sweep task re-dispatches it.
        logger.exception("Could not enqueue dispatch for payout batch %s.", batch_id)


@register_handler("payments.payout_batch")
class PayoutBatchApprovalHandler(BaseWorkflowHandler):
    """Approval handler for a bulk :class:`~vs_payments.models.PayoutBatch`."""

    allows_continue_without_approval = False

    # --- helpers ------------------------------------------------------------ #
    @property
    def document_model(self):  # Concrete model the engine's object_id points at.
        from .models import PayoutBatch
        return PayoutBatch

    def _load(self, instance):  # Row-lock the batch for a mutation.
        return self.document_model.objects.select_for_update().get(pk=instance.document_object_id)

    def _final_approver(self, instance):
        """The user whose approving vote completed the workflow (the checker).

        The engine's ``on_approved`` context does not carry the acting user, so we read
        it back from the immutable action log - the most recent non-reversed APPROVED
        vote on this instance, visible in the same transaction that recorded it. Returns
        ``None`` when no human voted; the dispatch service refuses that state.
        """
        from vs_workflow.models import WorkflowStageAction

        action = (
            WorkflowStageAction.objects
            .filter(stage_instance__instance=instance,
                    action=StageActionEnum.APPROVED,
                    reversed_at__isnull=True, is_reversal_of__isnull=True)
            .select_related("actor")
            .order_by("-acted_at", "-id")
            .first()
        )
        return action.actor if action is not None else None

    def _set_approval_status(self, instance, value):  # Record the approval phase in metadata.
        with transaction.atomic():
            batch = self._load(instance)
            meta = dict(batch.metadata or {})  # Copy so we never mutate in place.
            meta["approval_status"] = value  # PENDING_APPROVAL / APPROVED / DRAFT.
            batch.metadata = meta
            batch.save(update_fields=["metadata", "updated_at"])

    # --- engine entry points ------------------------------------------------ #
    def resolve_default_template_code(self, document) -> str:
        return "standard"  # One template code per document type for now.

    def validate_document(self, document, requested_by) -> None:
        """Reject anything that could not actually be submitted.

        Run at submission time so a doomed batch is refused before approvers spend
        effort: it must be a DRAFT batch that still has at least one PENDING
        instruction to dispatch.
        """
        from .constants import PayoutBatchStatus, PayoutStatus

        if document.status != PayoutBatchStatus.DRAFT:  # Only a draft batch can be gated.
            raise InvalidInstanceStateError("Only a draft payout batch can be submitted for approval.")
        if not document.instructions.filter(status=PayoutStatus.PENDING).exists():  # Nothing to dispatch.
            raise InvalidInstanceStateError("This batch has no pending instructions to submit.")

    def get_document_summary(self, document) -> dict:
        from vs_finance.money import format_naira

        return {  # Curated snapshot for the approval screen.
            "title": document.reference,  # The batch reference.
            "subtitle": "Bulk payout batch",  # Human label.
            "fields": [
                {"label": "Items", "value": str(document.item_count)},  # Number of beneficiaries.
                {"label": "Total", "value": format_naira(document.total_amount)},  # Total disbursed.
                {"label": "Provider", "value": document.provider},  # PSP the batch goes through.
            ],
            "link": f"/finance/payments/batches?{urlencode({'document': document.pk, 'entity': document.entity.code})}",
        }

    def on_submitted(self, instance, context) -> None:
        self._set_approval_status(instance, "PENDING_APPROVAL")  # Batch stays DRAFT, awaiting approval.

    def on_approved(self, instance, context) -> None:
        """Final approval records the decision; the provider is called after commit.

        This callback runs inside ``record_action``'s atomic block, and a bank transfer
        is not something that block can roll back. Dispatching from here would mean
        that a deadlock or a later failure anywhere in the approval transaction could
        undo the batch back to an undispatched DRAFT *after* the provider had already
        moved the money - money gone, no local record of it, and the next run free to
        send it again.

        So approval only marks the batch, and ``transaction.on_commit`` hands the
        dispatch to a worker once that decision is durably committed. If the enqueue
        never lands, ``vs_payments.dispatch_undispatched_payout_batches`` finds the
        batch and sends it.

        What *does* stay in this transaction is every check that costs nothing: stale
        vendor bank details, drifted totals, too few distinct human votes. Those still
        refuse the approval outright, so the approver is told why while they are still
        looking at the screen - rather than approving a batch that could only ever fail
        in a worker ten minutes later.
        """
        from .services import validate_payout_batch_dispatch

        batch = self._load(instance)  # Row-locked batch.
        validate_payout_batch_dispatch(batch, approved_instance=instance)  # Sends nothing.
        meta = dict(batch.metadata or {})
        meta["approval_status"] = "APPROVED"  # Record that approval completed.
        batch.metadata = meta
        batch.save(update_fields=["metadata", "updated_at"])

        # Read the deciding vote here: the action row is visible in this transaction.
        approver = self._final_approver(instance)
        batch_id = batch.pk
        # Stringify the ids: the broker serializer is JSON, and a WorkflowInstance pk
        # is a UUID. The service re-reads both, so string lookups are equivalent.
        instance_id = str(instance.pk)
        actor_id = str(approver.pk) if approver is not None else None
        transaction.on_commit(
            lambda: _enqueue_dispatch(batch_id, instance_id, actor_id),
        )

    def on_rejected(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Back to a plain draft.

    def on_returned(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Requester amends and resubmits.
