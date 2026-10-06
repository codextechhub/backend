"""Registry for named condition functions.

A condition may call a predicate by key (``{"fn": "has_open_fees", "args":
{...}}``) where a comparison of one field cannot say what is needed. The key is
for the server. Every screen that shows the condition shows what the function
checks instead, so a function registers with ``describe``: a sentence, or a
callable taking the condition's ``args`` and returning one ("Raised by someone
at Ikeja Branch"). :func:`describe_condition_function` answers that sentence,
and "A custom check" for a function registered without one, never its key.
"""
from typing import Any, Callable, Dict, Optional, Union
from vs_workflow.exceptions import ConditionFunctionAlreadyRegisteredError, UnknownConditionFunctionError

_REGISTRY: Dict[str, Callable[[Any, Optional[dict]], bool]] = {}
_DESCRIPTIONS: Dict[str, Union[str, Callable[[dict], str]]] = {}

#: What a function registered without a description reads as.
UNDESCRIBED_FUNCTION = "A custom check"


def register_condition(key: str, describe: Union[str, Callable[[dict], str], None] = None):
    """Register a named custom predicate usable from JSON route conditions."""
    def _decorate(fn: Callable[[Any, Optional[dict]], bool]):
        if key in _REGISTRY:
            if _REGISTRY[key] is fn:
                # Re-imports during app startup should not fail duplicate registration.
                return fn
            raise ConditionFunctionAlreadyRegisteredError(
                f"Condition function '{key}' already registered", key=key)
        _REGISTRY[key] = fn
        if describe is not None:
            _DESCRIPTIONS[key] = describe
        return fn
    return _decorate


def get_condition_function(key: str) -> Callable[[Any, Optional[dict]], bool]:
    """Fetch the custom predicate referenced by a route condition."""
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownConditionFunctionError(
            f"No condition function registered with key '{key}'", key=key)


def describe_condition_function(key: str, args: Optional[dict] = None) -> str:
    """What the function *key* checks, in words, for these *args*.

    A description that fails for these args falls back to the neutral phrase
    rather than failing the screen that shows it.
    """
    describe = _DESCRIPTIONS.get(key)
    if describe is None:
        return UNDESCRIBED_FUNCTION
    if isinstance(describe, str):
        return describe
    try:
        return str(describe(dict(args or {}))) or UNDESCRIBED_FUNCTION
    except Exception:  # noqa: BLE001 - a bad description must not break a read
        return UNDESCRIBED_FUNCTION


def list_registered_conditions() -> Dict[str, Callable]:
    """A copy of the registry, so callers cannot mutate it directly."""
    return dict(_REGISTRY)
