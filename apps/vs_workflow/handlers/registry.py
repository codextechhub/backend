"""Handler registry for document types."""
from typing import Dict, Type
from vs_workflow.exceptions import HandlerAlreadyRegisteredError, UnknownDocumentTypeError
from vs_workflow.handlers.base import BaseWorkflowHandler

_REGISTRY: Dict[str, BaseWorkflowHandler] = {}

# Register the document handler that owns a workflow document_type.
def register_handler(document_type: str):
    def _decorate(cls: Type[BaseWorkflowHandler]):
        if not issubclass(cls, BaseWorkflowHandler):
            raise TypeError(f"{cls.__name__} must subclass BaseWorkflowHandler")
        if document_type in _REGISTRY:
            existing = type(_REGISTRY[document_type])
            if existing is cls:
                # Re-imports during app startup should not fail duplicate registration.
                return cls
            raise HandlerAlreadyRegisteredError(
                f"Handler for '{document_type}' already registered as {existing.__name__}",
                document_type=document_type)
        instance = cls()
        instance.document_type = document_type
        _REGISTRY[document_type] = instance
        return cls
    return _decorate

# Fetch the handler that validates and reacts to a document type.
def get_handler(document_type: str) -> BaseWorkflowHandler:
    try:
        return _REGISTRY[document_type]
    except KeyError:
        raise UnknownDocumentTypeError(
            f"No handler registered for document_type '{document_type}'",
            document_type=document_type)

# Return a copy so callers cannot mutate the registry directly.
def list_registered_handlers() -> Dict[str, BaseWorkflowHandler]:
    return dict(_REGISTRY)
