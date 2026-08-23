"""Registry for named condition functions."""
from typing import Any, Callable, Dict, Optional
from vs_workflow.exceptions import ConditionFunctionAlreadyRegisteredError, UnknownConditionFunctionError

_REGISTRY: Dict[str, Callable[[Any, Optional[dict]], bool]] = {}

# Register a named custom predicate usable from JSON route conditions.
def register_condition(key: str):
    def _decorate(fn: Callable[[Any, Optional[dict]], bool]):
        if key in _REGISTRY:
            if _REGISTRY[key] is fn:
                # Re-imports during app startup should not fail duplicate registration.
                return fn
            raise ConditionFunctionAlreadyRegisteredError(
                f"Condition function '{key}' already registered", key=key)
        _REGISTRY[key] = fn
        return fn
    return _decorate

# Fetch the custom predicate referenced by a route condition.
def get_condition_function(key: str) -> Callable[[Any, Optional[dict]], bool]:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownConditionFunctionError(
            f"No condition function registered with key '{key}'", key=key)

# Return a copy so callers cannot mutate the condition registry directly.
def list_registered_conditions() -> Dict[str, Callable]:
    return dict(_REGISTRY)
