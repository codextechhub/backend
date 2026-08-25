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
