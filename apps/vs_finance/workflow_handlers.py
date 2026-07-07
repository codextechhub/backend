"""vs_workflow handlers for finance approval-gated documents.

Registering a handler per ``workflow_document_type`` is what lets the generic
``vs_workflow`` engine drive a finance document through approval without knowing
anything about the GL. The engine calls :meth:`resolve_default_template_code` to
pick the template, :meth:`validate_document` to reject a doomed document *before*
it enters the queue, :meth:`get_document_summary` to snapshot the approval screen,
and the ``on_*`` lifecycle callbacks on each transition.

**The golden rule (design §3): the GL posting happens inside ``on_approved``,
never before.** So money cannot hit the ledger until approval completes. The
posting call reuses the existing per-document-type service unchanged
(:func:`vs_finance.posting.post_journal` for journals,
:func:`vs_finance.credit_notes.post_refund` for refunds) — this module only moves
*when* it is called and *who* triggers it.

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
        self._mark_approved(doc)
        self.post(doc, actor_user=self._final_approver(instance))

    def _mark_approved(self, doc) -> None:
        """Flip the document to APPROVED before the GL posting (design §5).

        The intermediate APPROVED write is transient — the ``post`` call overwrites
        it with POSTED inside the same transaction. Subclasses whose posting service
        insists on a DRAFT document (e.g. the refund service re-guards ``status ==
        DRAFT``) override this to hand ``post`` a DRAFT document instead and let it
        drive DRAFT → POSTED.
        """
        doc.status = DocumentStatus.APPROVED
        doc.save(update_fields=["status", "updated_at"])

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


@register_handler("finance.refund")
class RefundHandler(_FinancePostOnApprove):
    """Approval handler for a customer :class:`~vs_finance.models.Refund` (cash out)."""

    @property
    def document_model(self):
        from .models import Refund
        return Refund

    def _mark_approved(self, doc) -> None:
        # post_refund owns the DRAFT → POSTED transition and re-guards status ==
        # DRAFT, so we hand it a DRAFT document rather than flipping to APPROVED
        # first. On approval the refund thus moves PENDING_APPROVAL → DRAFT → POSTED,
        # with post_refund driving the final POSTED write exactly as on the ungated
        # direct-post path. (If post_refund raises, this DRAFT write rolls back with
        # the whole approval action — Option A — so the doc is never left DRAFT.)
        if doc.status != DocumentStatus.DRAFT:
            doc.status = DocumentStatus.DRAFT
            doc.save(update_fields=["status", "updated_at"])

    def preflight(self, document) -> None:
        """Run the refund-posting guards without writing anything.

        Mirrors the guards in :func:`vs_finance.credit_notes._post_refund_atomic`
        (positive amount, amount within the customer's available credit, a
        resolvable deposit/bank account) with the same ``PostingError`` messages —
        so the preflight and the eventual post agree — but never mutates. The DRAFT
        check is handled by the base ``validate_document``.
        """
        from .exceptions import PostingError
        from .receivables import customer_credit_balance

        if document.amount <= 0:
            raise PostingError("A refund must have a positive amount to post.")

        available = customer_credit_balance(document.customer)
        if document.amount > available:
            raise PostingError(
                f"Refund of {document.amount} kobo exceeds {document.customer.code}'s "
                f"available credit ({available} kobo).",
            )

        deposit = document.deposit_account or (
            document.bank_account.gl_account if document.bank_account_id else None
        )
        if deposit is None:
            raise PostingError("Refund has no bank/deposit account to pay from.")

    def post(self, document, *, actor_user) -> None:
        from .credit_notes import post_refund

        post_refund(document, actor_user=actor_user)

    def summary(self, document) -> dict:
        return {
            "title": document.document_number or str(document.pk),
            "subtitle": "Customer refund",
            "fields": [
                {"label": "Date", "value": document.refund_date.isoformat()},
                {"label": "Customer", "value": document.customer.code},
                {"label": "Amount", "value": format_naira(document.amount)},
            ],
            "link": f"/finance/refunds/{document.pk}/",
        }


@register_handler("finance.write_off")
class WriteOffHandler(_FinancePostOnApprove):
    """Approval handler for a bad-debt :class:`~vs_finance.models.WriteOffRequest`.

    Unlike the refund handler, the default ``_mark_approved`` (flip to APPROVED
    before posting) is correct here: :func:`write_off_invoice` guards the *invoice*'s
    status, not the request's, and :func:`post_write_off_request` accepts an APPROVED
    request — so no DRAFT-override is needed.
    """

    @property
    def document_model(self):
        from .models import WriteOffRequest
        return WriteOffRequest

    def preflight(self, document) -> None:
        """Run the write-off guards without writing anything.

        Mirrors :func:`vs_finance.credit_notes._write_off_invoice_atomic` (invoice
        POSTED, outstanding balance > 0, effective amount positive and within the
        balance, customer has an AR control account) with the same ``PostingError``
        messages — so the preflight and the eventual post agree — but never mutates.
        """
        from .exceptions import PostingError

        invoice = document.invoice
        if invoice.status != DocumentStatus.POSTED:
            raise PostingError(
                f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
                f"only a posted invoice can be written off.",
            )

        balance = invoice.balance_due
        if balance <= 0:
            raise PostingError("Invoice has no outstanding balance to write off.")

        amount = balance if document.amount in (None, 0, "") else int(document.amount)
        if amount <= 0:
            raise PostingError("Write-off amount must be positive.")
        if amount > balance:
            raise PostingError(
                f"Write-off amount ({amount} kobo) exceeds the outstanding balance "
                f"({balance} kobo).",
            )

        if invoice.customer.receivable_account is None:
            raise PostingError(
                f"Customer {invoice.customer.code} has no receivable (AR control) account set.",
            )

    def post(self, document, *, actor_user) -> None:
        from .credit_notes import post_write_off_request

        post_write_off_request(document, actor_user=actor_user)

    def summary(self, document) -> dict:
        invoice = document.invoice
        amount = document.amount or invoice.balance_due
        return {
            "title": document.document_number or str(document.pk),
            "subtitle": "Bad-debt write-off",
            "fields": [
                {"label": "Invoice", "value": invoice.document_number or str(invoice.pk)},
                {"label": "Customer", "value": invoice.customer.code},
                {"label": "Amount", "value": format_naira(amount)},
                {"label": "Reason", "value": document.reason or "—"},
            ],
            "link": f"/finance/write-offs/{document.pk}/",
        }
