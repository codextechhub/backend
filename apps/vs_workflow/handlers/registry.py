"""Handler registry for document types."""
from typing import Dict, Type
from vs_workflow.constants import DocumentAudience
from vs_workflow.exceptions import (
    HandlerAlreadyRegisteredError, ReversalContractNotDeclaredError,
    UnknownDocumentTypeError,
)
from vs_workflow.handlers.base import BaseWorkflowHandler, declares_reversal_answer

_REGISTRY: Dict[str, BaseWorkflowHandler] = {}

# Register the document handler that owns a workflow document_type.
def register_handler(document_type: str):
    """Claim ``document_type`` for this handler, on two conditions.

    It must be a handler, and it must have said what an approval of its type
    releases. The second is checked here rather than at the moment somebody
    reverses one, because registration happens as the apps load: a type that
    has not answered stops the process that would serve it, instead of shipping
    and reversing silently until the day an administrator undoes an approval
    whose effect is still standing.

    See :func:`~vs_workflow.handlers.base.declares_reversal_answer` for the
    three ways a type answers.
    """
    def _decorate(cls: Type[BaseWorkflowHandler]):
        if not issubclass(cls, BaseWorkflowHandler):
            raise TypeError(f"{cls.__name__} must subclass BaseWorkflowHandler")
        if not declares_reversal_answer(cls):
            raise ReversalContractNotDeclaredError(
                f"{cls.__name__} has not said what approving a "
                f"'{document_type}' releases, so it cannot be registered. "
                f"Answer reversal_block_reason(document), or set "
                f"approval_releases_nothing = True where withdrawing the "
                f"engine's record of the vote is the whole of the change.",
                document_type=document_type,
            )
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


def raises(handler: BaseWorkflowHandler, tenant) -> bool:
    """Whether *tenant* raises documents of *handler*'s type, going by the tenant's kind."""
    audience = handler.audience
    return audience == DocumentAudience.ALL or audience == getattr(tenant, "kind", None)


def handlers_raised_by(tenant) -> Dict[str, BaseWorkflowHandler]:
    """The registered document types *tenant* raises, each with its handler."""
    return {t: handler for t, handler in _REGISTRY.items() if raises(handler, tenant)}
