"""vs_workflow handler for approval-gating bulk payout batches.

A :class:`~vs_payments.models.PayoutBatch` disburses money *out* to many
beneficiaries at once, so it is the highest-risk cash-out path — exactly the thing
a maker-checker gate belongs on. When a ``payments.payout_batch`` WorkflowTemplate
is published for a batch's ``(school, branch)`` scope, submitting the batch to the
provider happens only after approval; with no template, direct submit is unchanged
(opt-in by template, mirroring the finance approval slices).

Unlike a finance document, a payout batch is **not** a GL posting — approval gates
the *provider submission* (:func:`vs_payments.services.submit_payout_batch`), which
dispatches the batch's pending instructions to the PSP. The batch's own
``PayoutBatchStatus`` has no approval states, so we track the approval phase in
``metadata["approval_status"]`` and leave the batch ``DRAFT`` until it is approved
and submitted (at which point the service moves it to ``PROCESSING``).

Auto-discovered by the engine on startup via ``autodiscover_modules("workflow_handlers")``.
"""
from __future__ import annotations

from django.db import transaction

from vs_workflow.constants import WorkflowStageAction as StageActionEnum
from vs_workflow.exceptions import InvalidInstanceStateError
from vs_workflow.handlers import BaseWorkflowHandler, register_handler


@register_handler("payments.payout_batch")
class PayoutBatchApprovalHandler(BaseWorkflowHandler):
    """Approval handler for a bulk :class:`~vs_payments.models.PayoutBatch`."""

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
        it back from the immutable action log — the most recent non-reversed APPROVED
        vote on this instance, visible in the same transaction that recorded it. Falls
        back to the requester only if no human ever voted (a fully auto-skipped template).
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
        return action.actor if action is not None else instance.requested_by

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
            "link": f"/payments/payout-batches/{document.pk}/",  # Deep link to the batch.
        }

    def on_submitted(self, instance, context) -> None:
        self._set_approval_status(instance, "PENDING_APPROVAL")  # Batch stays DRAFT, awaiting approval.

    def on_approved(self, instance, context) -> None:
        """Final approval → dispatch the batch to the provider.

        Runs inside ``record_action``'s atomic block. ``submit_payout_batch`` is
        itself best-effort per instruction (a per-item provider rejection marks that
        child FAILED but does not raise), so approval reliably results in a submitted
        batch; individual failures surface in the batch's recomputed status.
        """
        from .services import submit_payout_batch

        batch = self._load(instance)  # Row-locked batch.
        meta = dict(batch.metadata or {})
        meta["approval_status"] = "APPROVED"  # Record that approval completed.
        batch.metadata = meta
        batch.save(update_fields=["metadata", "updated_at"])
        submit_payout_batch(batch, actor_user=self._final_approver(instance))  # Dispatch to the PSP.

    def on_rejected(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Back to a plain draft.

    def on_returned(self, instance, context) -> None:
        self._set_approval_status(instance, "DRAFT")  # Requester amends and resubmits.
