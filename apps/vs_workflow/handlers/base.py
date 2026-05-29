"""BaseWorkflowHandler — subclass this in workflow_handlers.py of your app."""
from typing import Any, Dict, Optional, Type

class BaseWorkflowHandler:
    document_type: str = ""
    document_model: Optional[Type] = None

    def resolve_default_template_code(self, document: Any) -> str:
        raise NotImplementedError("Subclasses must implement resolve_default_template_code().")

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

        Default is empty — override to surface details.
        """
        return {}

    def on_submitted(self, instance, context: Dict) -> None: ...
    def on_approved(self, instance, context: Dict) -> None: ...
    def on_rejected(self, instance, context: Dict) -> None: ...
    def on_returned(self, instance, context: Dict) -> None: ...
    def on_withdrawn(self, instance, context: Dict) -> None: ...
    def on_cancelled(self, instance, context: Dict) -> None: ...
