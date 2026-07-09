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
from __future__ import annotations  # Defer annotation evaluation during workflow autodiscovery.

from django.db import transaction  # Keeps status transitions atomic.

from vs_workflow.constants import WorkflowStageAction as StageActionEnum  # Action enum used to find final approver.
from vs_workflow.exceptions import InvalidInstanceStateError  # Raised for invalid workflow submissions.
from vs_workflow.handlers import BaseWorkflowHandler, register_handler  # Handler base and registration decorator.

from .constants import DocumentStatus  # Finance document lifecycle statuses.
from .money import format_naira  # Formats kobo totals in approval summaries.


class _FinancePostOnApprove(BaseWorkflowHandler):  # Shared handler for finance docs that post after approval.
    """Shared base: submit → PENDING_APPROVAL; approve → APPROVED then post; reject/return → DRAFT.

    Subclasses supply the concrete model (``document_model``) and the three
    document-type hooks — :meth:`preflight` (the write-free posting guards),
    :meth:`post` (the real GL posting) and :meth:`summary` (the approval-screen
    snapshot). Everything else is uniform across finance document types.
    """

    #: Template code resolved for every finance document unless overridden.
    default_template_code = "standard"  # Default workflow template code for finance documents.

    def resolve_default_template_code(self, document) -> str:  # Tell workflow which template to use.
        return self.default_template_code  # Return the finance default unless subclass overrides.

    # --- helpers ------------------------------------------------------------ #
    def _load(self, instance):  # Reload and lock the document represented by a workflow instance.
        """Re-load and row-lock the concrete document for a mutation."""
        return self.document_model.objects.select_for_update().get(pk=instance.document_object_id)  # Lock row for status/post mutation.

    def _final_approver(self, instance):  # Resolve the user whose vote completed approval.
        """The user whose approving vote completed the workflow (the checker).

        The engine's ``on_approved`` context does not carry the acting user, so we
        read it back from the immutable action log: the most recent non-reversed
        APPROVED ``WorkflowStageAction`` on this instance. This runs inside the same
        transaction that recorded that vote, so the row is visible. Falls back to
        ``instance.requested_by`` only if — for a fully auto-skipped template — no
        human ever voted.
        """
        from vs_workflow.models import WorkflowStageAction  # Local import avoids workflow model load at module import.

        action = (  # Find latest unreversed approval action on this instance.
            WorkflowStageAction.objects  # Start from workflow stage action log.
            .filter(stage_instance__instance=instance,  # Restrict to this workflow instance.
                    action=StageActionEnum.APPROVED,  # Only approval votes can complete approval.
                    reversed_at__isnull=True, is_reversal_of__isnull=True)  # Ignore reversed/reversal actions.
            .select_related("actor")  # Load actor for return without extra query.
            .order_by("-acted_at", "-id")  # Latest action wins.
            .first()  # Return one action or None.
        )  # Close the grouped expression.
        return action.actor if action is not None else instance.requested_by  # Fallback to requester for auto-skipped flows.

    # --- engine entry points ------------------------------------------------ #
    def validate_document(self, document, requested_by) -> None:  # Validate a document before workflow submission.
        """Reject anything but a DRAFT, then run the posting preflight (no writes).

        Running the posting guards at submission time means a document that could
        never post — unbalanced, empty, into a closed period, or touching an
        inactive account — is refused before it ever enters the approval queue,
        rather than failing at ``on_approved`` after approvers have spent effort.
        """
        if getattr(document, "status", None) != DocumentStatus.DRAFT:  # Only draft finance docs enter approval.
            raise InvalidInstanceStateError("Only a draft can be submitted for approval.")
        self.preflight(document)  # Run write-free posting guards.

    def get_document_summary(self, document) -> dict:  # Build approval-screen snapshot.
        return self.summary(document)  # Delegate to document-specific summary.

    def on_submitted(self, instance, context) -> None:  # Move document into pending approval state.
        with transaction.atomic():  # Keep load and status save atomic.
            doc = self._load(instance)  # Lock the concrete finance document.
            doc.status = DocumentStatus.PENDING_APPROVAL  # Mark it waiting for approval.
            doc.save(update_fields=["status", "updated_at"])  # Persist status change only.

    def on_approved(self, instance, context) -> None:  # Post the document when workflow reaches approval.
        # Runs inside record_action's atomic block; a posting failure here rolls the
        # whole approval action back and leaves the stage ACTIVE (Option A).  # Preserve workflow consistency.
        doc = self._load(instance)  # Lock the concrete finance document.
        self._mark_approved(doc)  # Move it to the approved intermediate state.
        self.post(doc, actor_user=self._final_approver(instance))  # Run document-specific posting as final approver.

    def _mark_approved(self, doc) -> None:  # Mark document approved before posting.
        """Flip the document to APPROVED before the GL posting (design §5).

        The intermediate APPROVED write is transient — the ``post`` call overwrites
        it with POSTED inside the same transaction. Subclasses whose posting service
        insists on a DRAFT document (e.g. the refund service re-guards ``status ==
        DRAFT``) override this to hand ``post`` a DRAFT document instead and let it
        drive DRAFT → POSTED.
        """
        doc.status = DocumentStatus.APPROVED  # Set intermediate approved status.
        doc.save(update_fields=["status", "updated_at"])  # Persist lifecycle transition.

    def on_rejected(self, instance, context) -> None:  # Return document to draft when rejected.
        with transaction.atomic():  # Keep load and status save atomic.
            doc = self._load(instance)  # Lock the concrete finance document.
            doc.status = DocumentStatus.DRAFT  # Rejected documents become editable drafts.
            doc.save(update_fields=["status", "updated_at"])  # Persist status change only.

    def on_returned(self, instance, context) -> None:  # Return document to draft when sent back.
        with transaction.atomic():  # Keep load and status save atomic.
            doc = self._load(instance)  # Lock the concrete finance document.
            doc.status = DocumentStatus.DRAFT  # Returned documents become editable drafts.
            doc.save(update_fields=["status", "updated_at"])  # Persist status change only.

    # --- document-type hooks (subclasses implement) ------------------------- #
    def preflight(self, document) -> None:  # pragma: no cover - abstract  # Subclasses validate without writes.
        raise NotImplementedError  # Raise the domain error for this path.

    def post(self, document, *, actor_user):  # pragma: no cover - abstract  # Subclasses perform the real posting.
        raise NotImplementedError  # Raise the domain error for this path.

    def summary(self, document) -> dict:  # pragma: no cover - abstract  # Subclasses shape approval summary data.
        raise NotImplementedError  # Raise the domain error for this path.


@register_handler("finance.journal")
class JournalHandler(_FinancePostOnApprove):  # Workflow handler for manual journal approvals.
    """Approval handler for a manual :class:`~vs_finance.models.JournalEntry`."""

    @property  # Apply the decorator to this callable.
    def document_model(self):  # Concrete model for finance.journal instances.
        from .models import JournalEntry  # Local import avoids model import cycles.
        return JournalEntry  # Return journal model class.

    def preflight(self, document) -> None:  # Validate journal can post before it enters workflow.
        """Run the posting guards without writing anything.

        Reuses the exact guards :func:`vs_finance.posting.post_journal` applies at
        post time (period resolvable + open, ≥1 line, balanced, every account
        active + postable) so the preflight and the eventual post agree — the only
        difference is this one never mutates.
        """
        from .posting import ensure_balanced, ensure_period_open, sum_sides  # Posting guard helpers.

        ensure_period_open(document.period)  # Reject journals into closed/restricted periods.

        lines = list(document.lines.select_related("account").all())  # Load journal lines and accounts.
        if not lines:  # Journals need at least one line.
            from .exceptions import PostingError  # Local import keeps error dependency narrow.
            raise PostingError("A journal must have at least one line to post.")

        total_debit, total_credit = sum_sides(lines)  # Calculate journal sides.
        ensure_balanced(total_debit, total_credit)  # Reject unbalanced journals.

        for line in lines:  # Validate every posting account.
            account = line.account  # Account on this journal line.
            if not (account.is_active and account.is_postable):  # Posting requires active leaf accounts.
                from .exceptions import InactiveAccountError  # Local import keeps error dependency narrow.
                raise InactiveAccountError(account_code=account.code)  # Raise the domain error for this path.

    def post(self, document, *, actor_user) -> None:  # Post an approved journal.
        from .posting import post_journal  # Real journal posting service.

        post_journal(document, actor_user=actor_user)  # Delegate mutation to posting service.

    def summary(self, document) -> dict:  # Build approval summary for a journal.
        return {  # Workflow summary payload.
            "title": document.document_number or str(document.pk),  # Display document number or id.
            "subtitle": "Journal entry",  # Document type label.
            "fields": [  # Key facts shown to approvers.
                {"label": "Date", "value": document.date.isoformat()},  # Journal date.
                {"label": "Narration", "value": document.narration or "—"},  # Journal narration.
                {"label": "Total", "value": format_naira(document.total_debit_kobo)},  # Journal total.
            ],  # Close the grouped value.
            "link": f"/finance/journals/{document.pk}/",  # Frontend deep link.
        }  # Close the grouped expression.


@register_handler("finance.refund")
class RefundHandler(_FinancePostOnApprove):  # Workflow handler for customer refund approvals.
    """Approval handler for a customer :class:`~vs_finance.models.Refund` (cash out)."""

    @property  # Apply the decorator to this callable.
    def document_model(self):  # Concrete model for finance.refund instances.
        from .models import Refund  # Local import avoids model import cycles.
        return Refund  # Return refund model class.

    def _mark_approved(self, doc) -> None:  # Refund posting service requires a draft document.
        # post_refund owns the DRAFT → POSTED transition and re-guards status ==
        # DRAFT, so we hand it a DRAFT document rather than flipping to APPROVED
        # first. On approval the refund thus moves PENDING_APPROVAL → DRAFT → POSTED,
        # with post_refund driving the final POSTED write exactly as on the ungated
        # direct-post path. (If post_refund raises, this DRAFT write rolls back with
        # the whole approval action — Option A — so the doc is never left DRAFT.)  # Preserve service invariants.
        if doc.status != DocumentStatus.DRAFT:  # Reset only when currently pending/approved.
            doc.status = DocumentStatus.DRAFT  # Hand draft state to post_refund.
            doc.save(update_fields=["status", "updated_at"])  # Persist temporary status inside transaction.

    def preflight(self, document) -> None:  # Validate refund can post before it enters workflow.
        """Run the refund-posting guards without writing anything.

        Mirrors the guards in :func:`vs_finance.credit_notes._post_refund_atomic`
        (positive amount, amount within the customer's available credit, a
        resolvable deposit/bank account) with the same ``PostingError`` messages —
        so the preflight and the eventual post agree — but never mutates. The DRAFT
        check is handled by the base ``validate_document``.
        """
        from .exceptions import PostingError  # Error type matching posting service.
        from .receivables import customer_credit_balance  # Computes refundable credit.

        if document.amount <= 0:  # Refunds must pay out a positive amount.
            raise PostingError("A refund must have a positive amount to post.")

        available = customer_credit_balance(document.customer)  # Current customer credit balance.
        if document.amount > available:  # Cannot refund more than available credit.
            raise PostingError(  # Raise the domain error for this path.
                f"Refund of {document.amount} kobo exceeds {document.customer.code}'s "
                f"available credit ({available} kobo).",
            )  # Close the grouped expression.

        deposit = document.deposit_account or (  # Resolve source account for cash out.
            document.bank_account.gl_account if document.bank_account_id else None  # Fallback to bank GL account.
        )  # Close the grouped expression.
        if deposit is None:  # Refund cannot post without a bank/deposit account.
            raise PostingError("Refund has no bank/deposit account to pay from.")

    def post(self, document, *, actor_user) -> None:  # Post an approved refund.
        from .credit_notes import post_refund  # Real refund posting service.

        post_refund(document, actor_user=actor_user)  # Delegate mutation to credit-note service.

    def summary(self, document) -> dict:  # Build approval summary for a refund.
        return {  # Workflow summary payload.
            "title": document.document_number or str(document.pk),  # Display document number or id.
            "subtitle": "Customer refund",  # Document type label.
            "fields": [  # Key facts shown to approvers.
                {"label": "Date", "value": document.refund_date.isoformat()},  # Refund date.
                {"label": "Customer", "value": document.customer.code},  # Customer code.
                {"label": "Amount", "value": format_naira(document.amount)},  # Refund amount.
            ],  # Close the grouped value.
            "link": f"/finance/refunds/{document.pk}/",  # Frontend deep link.
        }  # Close the grouped expression.


@register_handler("finance.write_off")
class WriteOffHandler(_FinancePostOnApprove):  # Workflow handler for bad-debt write-off approvals.
    """Approval handler for a bad-debt :class:`~vs_finance.models.WriteOffRequest`.

    Unlike the refund handler, the default ``_mark_approved`` (flip to APPROVED
    before posting) is correct here: :func:`write_off_invoice` guards the *invoice*'s
    status, not the request's, and :func:`post_write_off_request` accepts an APPROVED
    request — so no DRAFT-override is needed.
    """

    @property  # Apply the decorator to this callable.
    def document_model(self):  # Concrete model for finance.write_off instances.
        from .models import WriteOffRequest  # Local import avoids model import cycles.
        return WriteOffRequest  # Return write-off request model class.

    def preflight(self, document) -> None:  # Validate write-off can post before workflow submission.
        """Run the write-off guards without writing anything.

        Mirrors :func:`vs_finance.credit_notes._write_off_invoice_atomic` (invoice
        POSTED, outstanding balance > 0, effective amount positive and within the
        balance, customer has an AR control account) with the same ``PostingError``
        messages — so the preflight and the eventual post agree — but never mutates.
        """
        from .exceptions import PostingError  # Error type matching posting service.

        invoice = document.invoice  # Invoice targeted by the write-off request.
        if invoice.status != DocumentStatus.POSTED:  # Only posted invoices have AR balances.
            raise PostingError(  # Raise the domain error for this path.
                f"Invoice {invoice.document_number or invoice.pk} is '{invoice.status}'; "
                f"only a posted invoice can be written off.",
            )  # Close the grouped expression.

        balance = invoice.balance_due  # Current outstanding invoice balance.
        if balance <= 0:  # Fully settled invoices cannot be written off.
            raise PostingError("Invoice has no outstanding balance to write off.")

        amount = balance if document.amount in (None, 0, "") else int(document.amount)  # Default blank amount to full balance.
        if amount <= 0:  # Write-off amount must reduce a real balance.
            raise PostingError("Write-off amount must be positive.")
        if amount > balance:  # Cannot write off more than the invoice balance.
            raise PostingError(  # Raise the domain error for this path.
                f"Write-off amount ({amount} kobo) exceeds the outstanding balance "
                f"({balance} kobo).",
            )  # Close the grouped expression.

        if invoice.customer.receivable_account is None:  # AR control account is required for the reversal.
            raise PostingError(  # Raise the domain error for this path.
                f"Customer {invoice.customer.code} has no receivable (AR control) account set.",
            )  # Close the grouped expression.

    def post(self, document, *, actor_user) -> None:  # Post an approved write-off request.
        from .credit_notes import post_write_off_request  # Real write-off posting service.

        post_write_off_request(document, actor_user=actor_user)  # Delegate mutation to credit-note service.

    def summary(self, document) -> dict:  # Build approval summary for a write-off.
        invoice = document.invoice  # Invoice targeted by the write-off.
        amount = document.amount or invoice.balance_due  # Blank request amount means full balance.
        return {  # Workflow summary payload.
            "title": document.document_number or str(document.pk),  # Display document number or id.
            "subtitle": "Bad-debt write-off",  # Document type label.
            "fields": [  # Key facts shown to approvers.
                {"label": "Invoice", "value": invoice.document_number or str(invoice.pk)},  # Target invoice.
                {"label": "Customer", "value": invoice.customer.code},  # Customer code.
                {"label": "Amount", "value": format_naira(amount)},  # Write-off amount.
                {"label": "Reason", "value": document.reason or "—"},  # Request reason.
            ],  # Close the grouped value.
            "link": f"/finance/write-offs/{document.pk}/",  # Frontend deep link.
        }  # Close the grouped expression.
