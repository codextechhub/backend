"""BaseWorkflowHandler - subclass this in workflow_handlers.py of your app."""
from typing import Any, Dict, Optional, Type

# Contract each app implements to connect documents to the workflow engine.
class BaseWorkflowHandler:
    document_type: str = ""
    document_model: Optional[Type] = None
    # Most document types may use the generic release for an unstaffed stage. A
    # handler can turn it off when terminal approval is itself a safety boundary.
    allows_continue_without_approval: bool = True

    # Choose the template code when the submitter does not provide one.
    def resolve_default_template_code(self, document: Any) -> str:
        raise NotImplementedError("Subclasses must implement resolve_default_template_code().")

    # Enforce document-specific submit guards before a workflow instance is created.
    def validate_document(self, document: Any, requested_by) -> None:
        return None

    def get_document_summary(self, document: Any) -> Dict:
        """Curated, display-only snapshot of the business document for approval UIs.

        The engine does not know the shape of any document, so each module
        describes its own. Snapshotted onto the WorkflowInstance at submission
        time, so the approval screen shows what was submitted even if the source
        document later changes.

        Convention (all keys optional):
            {
              "title": str,
              "subtitle": str,
              "fields": [{"label": str, "value": str}, ...],
              "link": str,   # optional deep link to the source record
            }

        Default is empty - override to surface details.
        """
        return {}

    def get_source_document_link(self, document: Any) -> Optional[str]:
        """Return the current console route for the source record, when one exists.

        Summary fields are an immutable submission snapshot, but navigation is
        live application metadata. Resolving the link again on detail reads lets
        route repairs and entity scope apply to approvals created in the past.
        """
        summary = self.get_document_summary(document)
        link = summary.get("link") if isinstance(summary, dict) else None
        return link if isinstance(link, str) and link else None

    # Lifecycle callbacks let the source app mirror workflow outcomes on its document.
    def on_submitted(self, instance, context: Dict) -> None: ...
    def on_approved(self, instance, context: Dict) -> None: ...
    def on_rejected(self, instance, context: Dict) -> None: ...
    def on_returned(self, instance, context: Dict) -> None: ...
    def on_withdrawn(self, instance, context: Dict) -> None: ...
    def on_cancelled(self, instance, context: Dict) -> None: ...

    # Reversal is the one outcome the engine cannot decide on its own.
    def validate_reversal(self, instance, context: Dict) -> None:
        """Refuse an administrator's reversal that this document cannot honour.

        Runs before the engine writes anything, so raising here leaves the
        approval, its stages and its votes exactly as they were. The engine can
        undo its own record of a decision; whether that decision has already had
        an effect outside the engine is knowledge only the owning module holds.

        A dispatched payout is the case this exists for. Once the provider holds
        the instructions the money is gone, and an instance moved back to
        IN_PROGRESS would describe a batch that is being paid as one still
        waiting for a decision.

        ``context`` carries ``action_id``, ``original_action``, ``stage_code``,
        ``attempt``, ``reason``, ``actor_id``, and ``was_final_approval`` - True
        when the instance stood fully APPROVED at the moment the reversal was
        asked for. Raise
        :class:`~vs_workflow.exceptions.ReversalNotAllowedError` to refuse.
        """
        return None

    def on_action_reversed(self, instance, context: Dict) -> None:
        """Put the document back after the engine has undone a decision.

        The counterpart of :meth:`on_approved`, :meth:`on_rejected` and
        :meth:`on_returned`: whatever those wrote about an outcome that no longer
        holds belongs back where it was. Runs inside the reversal transaction, so
        raising rolls the reversal back rather than leaving the document and the
        workflow disagreeing about the same decision.

        Called only when the reversal changed the workflow's outcome. A vote the
        stage did not need is voided without reopening anything, and a document
        the engine has not moved is not one this has to move either.

        ``context`` carries the keys :meth:`validate_reversal` receives, plus
        ``reopened_stage_code`` - the stage the instance returned to - and
        ``unwound_stages``, the codes of the stages downstream of it that the
        engine rolled back.
        """
        return None
