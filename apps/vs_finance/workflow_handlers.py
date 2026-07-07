"""vs_workflow handlers for finance approval-gated documents.

Registering a handler per ``workflow_document_type`` is what lets the generic
``vs_workflow`` engine drive a finance document through approval without knowing
anything about the GL. The engine calls :meth:`resolve_default_template_code` to
pick the template, :meth:`validate_document` to reject a doomed document *before*
it enters the queue, :meth:`get_document_summary` to snapshot the approval screen,
and the ``on_*`` lifecycle callbacks on each transition.

**The golden rule (design §3): the GL posting happens inside ``on_approved``,
never before.** So money cannot hit the ledger until approval completes. The
posting call reuses the existing :func:`vs_finance.posting.post_journal` service
unchanged — this module only moves *when* it is called and *who* triggers it.

**Post-failure behaviour — Option A (design §12 Q4).** The engine records an
approver's vote inside ``record_action``'s ``transaction.atomic`` block, and the
final approval reaches ``on_approved`` via ``advance_instance`` →
``_terminate_approved`` inside that *same* transaction. So if ``post_journal``
raises here (e.g. the period closed while the journal sat in the queue), the whole
approval action rolls back: the vote is not persisted and the stage stays ACTIVE,
with the finance error surfaced to the approver to retry once the block clears.
No new engine state is needed.

These handlers are auto-discovered by the engine on startup via
``autodiscover_modules("workflow_handlers")`` in ``VsWorkflowConfig.ready()``.
"""
from __future__ import annotations

from django.db import transaction

from vs_workflow.constants import WorkflowStageAction as StageActionEnum
from vs_workflow.exceptions import InvalidInstanceStateError
from vs_workflow.handlers import BaseWorkflowHandler, register_handler

from .constants import DocumentStatus
from .money import format_naira


class _FinancePostOnApprove(BaseWorkflowHandler):
    """Shared base: submit → PENDING_APPROVAL; approve → APPROVED then post; reject/return → DRAFT.

    Subclasses supply the concrete model (``document_model``) and the three
    document-type hooks — :meth:`preflight` (the write-free posting guards),
    :meth:`post` (the real GL posting) and :meth:`summary` (the approval-screen
    snapshot). Everything else is uniform across finance document types.
    """

    #: Template code resolved for every finance document unless overridden.
    default_template_code = "standard"

    def resolve_default_template_code(self, document) -> str:
        return self.default_template_code

    # --- helpers ------------------------------------------------------------ #
    def _load(self, instance):
        """Re-load and row-lock the concrete document for a mutation."""
        return self.document_model.objects.select_for_update().get(pk=instance.document_object_id)

    def _final_approver(self, instance):
        """The user whose approving vote completed the workflow (the checker).

        The engine's ``on_approved`` context does not carry the acting user, so we
        read it back from the immutable action log: the most recent non-reversed
        APPROVED ``WorkflowStageAction`` on this instance. This runs inside the same
        transaction that recorded that vote, so the row is visible. Falls back to
        ``instance.requested_by`` only if — for a fully auto-skipped template — no
        human ever voted.
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

    # --- engine entry points ------------------------------------------------ #
    def validate_document(self, document, requested_by) -> None:
        """Reject anything but a DRAFT, then run the posting preflight (no writes).

        Running the posting guards at submission time means a document that could
        never post — unbalanced, empty, into a closed period, or touching an
        inactive account — is refused before it ever enters the approval queue,
        rather than failing at ``on_approved`` after approvers have spent effort.
        """
        if getattr(document, "status", None) != DocumentStatus.DRAFT:
            raise InvalidInstanceStateError("Only a draft can be submitted for approval.")
        self.preflight(document)

    def get_document_summary(self, document) -> dict:
        return self.summary(document)

    def on_submitted(self, instance, context) -> None:
        with transaction.atomic():
            doc = self._load(instance)
            doc.status = DocumentStatus.PENDING_APPROVAL
            doc.save(update_fields=["status", "updated_at"])

    def on_approved(self, instance, context) -> None:
        # Runs inside record_action's atomic block; a posting failure here rolls the
        # whole approval action back and leaves the stage ACTIVE (Option A).
        doc = self._load(instance)
        doc.status = DocumentStatus.APPROVED
        doc.save(update_fields=["status", "updated_at"])
        self.post(doc, actor_user=self._final_approver(instance))

    def on_rejected(self, instance, context) -> None:
        with transaction.atomic():
            doc = self._load(instance)
            doc.status = DocumentStatus.DRAFT
            doc.save(update_fields=["status", "updated_at"])

    def on_returned(self, instance, context) -> None:
        with transaction.atomic():
            doc = self._load(instance)
            doc.status = DocumentStatus.DRAFT
            doc.save(update_fields=["status", "updated_at"])

    # --- document-type hooks (subclasses implement) ------------------------- #
    def preflight(self, document) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def post(self, document, *, actor_user):  # pragma: no cover - abstract
        raise NotImplementedError

    def summary(self, document) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


@register_handler("finance.journal")
class JournalHandler(_FinancePostOnApprove):
    """Approval handler for a manual :class:`~vs_finance.models.JournalEntry`."""

    @property
    def document_model(self):
        from .models import JournalEntry
        return JournalEntry

    def preflight(self, document) -> None:
        """Run the posting guards without writing anything.

        Reuses the exact guards :func:`vs_finance.posting.post_journal` applies at
        post time (period resolvable + open, ≥1 line, balanced, every account
        active + postable) so the preflight and the eventual post agree — the only
        difference is this one never mutates.
        """
        from .posting import ensure_balanced, ensure_period_open, sum_sides

        ensure_period_open(document.period)

        lines = list(document.lines.select_related("account").all())
        if not lines:
            from .exceptions import PostingError
            raise PostingError("A journal must have at least one line to post.")

        total_debit, total_credit = sum_sides(lines)
        ensure_balanced(total_debit, total_credit)

        for line in lines:
            account = line.account
            if not (account.is_active and account.is_postable):
                from .exceptions import InactiveAccountError
                raise InactiveAccountError(account_code=account.code)

    def post(self, document, *, actor_user) -> None:
        from .posting import post_journal

        post_journal(document, actor_user=actor_user)

    def summary(self, document) -> dict:
        return {
            "title": document.document_number or str(document.pk),
            "subtitle": "Journal entry",
            "fields": [
                {"label": "Date", "value": document.date.isoformat()},
                {"label": "Narration", "value": document.narration or "—"},
                {"label": "Total", "value": format_naira(document.total_debit_kobo)},
            ],
            "link": f"/finance/journals/{document.pk}/",
        }
