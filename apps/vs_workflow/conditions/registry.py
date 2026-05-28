"""Registry for named condition functions."""
from typing import Any, Callable, Dict, Optional
from vs_workflow.exceptions import ConditionFunctionAlreadyRegisteredError, UnknownConditionFunctionError

_REGISTRY: Dict[str, Callable[[Any, Optional[dict]], bool]] = {}

def register_condition(key: str):
    def _decorate(fn: Callable[[Any, Optional[dict]], bool]):
        if key in _REGISTRY:
            if _REGISTRY[key] is fn:
                return fn
            raise ConditionFunctionAlreadyRegisteredError(
                f"Condition function '{key}' already registered", key=key)
        _REGISTRY[key] = fn
        return fn
    return _decorate

def get_condition_function(key: str) -> Callable[[Any, Optional[dict]], bool]:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownConditionFunctionError(
            f"No condition function registered with key '{key}'", key=key)

def list_registered_conditions() -> Dict[str, Callable]:
    return dict(_REGISTRY)
