"""JSON condition evaluator with trace output.

The trace is stored on the route-evaluation audit entry and returned by the
builder's previews, so a person can read it. A check that fails to run is
recorded in it as :data:`CHECK_FAILED`, never as the exception: the exception's
class and text describe the server's code, and are logged here for whoever
maintains it.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Tuple
from vs_workflow.constants import (
    CONDITION_OP_CONTAINS, CONDITION_OP_EQ, CONDITION_OP_GT, CONDITION_OP_GTE,
    CONDITION_OP_IN, CONDITION_OP_LT, CONDITION_OP_LTE, CONDITION_OP_NE,
    CONDITION_OP_NOT_IN, CONDITION_OPERATORS,
)
from vs_workflow.exceptions import TemplateInvalidError, UnknownOperatorError
from vs_workflow.conditions.registry import get_condition_function

_MISSING = object()

logger = logging.getLogger(__name__)

#: What a trace says about a check that raised instead of answering.
CHECK_FAILED = "This check could not be answered for this document, so it counted as not met."

# Resolve dotted paths across dicts and model-like objects without raising.
def _extract_field(document: Any, path: str) -> Any:
    current = document
    for segment in path.split("."):
        if current is _MISSING or current is None:
            return _MISSING
        if isinstance(current, dict):
            current = current.get(segment, _MISSING)
        else:
            current = getattr(current, segment, _MISSING)
    return current

# Normalize numeric values so JSON numbers compare cleanly with Decimal model fields.
def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    return value

# Apply one supported comparison operator.
def _apply_op(op: str, left: Any, right: Any) -> bool:
    if op == CONDITION_OP_EQ:    return _normalise(left) == _normalise(right)
    if op == CONDITION_OP_NE:    return _normalise(left) != _normalise(right)
    if op == CONDITION_OP_GT:    return _normalise(left) >  _normalise(right)
    if op == CONDITION_OP_GTE:   return _normalise(left) >= _normalise(right)
    if op == CONDITION_OP_LT:    return _normalise(left) <  _normalise(right)
    if op == CONDITION_OP_LTE:   return _normalise(left) <= _normalise(right)
    if op == CONDITION_OP_IN:    return left in (right or [])
    if op == CONDITION_OP_NOT_IN: return left not in (right or [])
    if op == CONDITION_OP_CONTAINS:
        if left is None: return False
        return right in left
    raise UnknownOperatorError(f"Unknown operator '{op}'", op=op)

# Convert trace values into JSON-safe output for audit/debug payloads.
def _safe(v: Any):
    if isinstance(v, Decimal): return str(v)
    try:
        import json; json.dumps(v); return v
    except (TypeError, ValueError): return str(v)

# Evaluate a route condition and return both the boolean result and trace.
def evaluate_condition(condition: Any, document: Any) -> Tuple[bool, Dict]:
    if condition in (None, {}):
        return True, {"kind": "empty", "result": True}
    if not isinstance(condition, dict):
        raise TemplateInvalidError("Condition must be a JSON object or null")
    if "all" in condition:
        children = condition["all"] or []
        child_traces, result = [], True
        for child in children:
            r, t = evaluate_condition(child, document)
            child_traces.append(t)
            if not r: result = False
        return result, {"kind": "all", "children": child_traces, "result": result}
    if "any" in condition:
        children = condition["any"] or []
        child_traces, result = [], False
        for child in children:
            r, t = evaluate_condition(child, document)
            child_traces.append(t)
            if r: result = True
        return result, {"kind": "any", "children": child_traces, "result": result}
    if "not" in condition:
        r, t = evaluate_condition(condition["not"], document)
        return (not r), {"kind": "not", "child": t, "result": (not r)}
    if "fn" in condition:
        key = condition["fn"]; args = condition.get("args") or {}
        fn = get_condition_function(key)
        try:
            result = bool(fn(document, args))
        except Exception:
            # A failing custom check fails closed; the reason goes to the log.
            logger.exception("Condition function %r raised", key)
            return False, {"kind": "fn", "fn": key, "args": args, "result": False,
                           "error": CHECK_FAILED}
        return result, {"kind": "fn", "fn": key, "args": args, "result": result}
    if "op" in condition:
        op = condition["op"]
        if op not in CONDITION_OPERATORS:
            raise UnknownOperatorError(f"Unknown operator '{op}'", op=op)
        field_path = condition.get("field")
        if not field_path:
            raise TemplateInvalidError("Operator condition missing 'field'")
        value = condition.get("value")
        extracted = _extract_field(document, field_path)
        # Missing fields compare as None so the trace stays explicit.
        left = None if extracted is _MISSING else extracted
        try:
            result = _apply_op(op, left, value)
        except TypeError:
            logger.info("Condition on %r could not compare %r with %r", field_path, left, value)
            return False, {"kind": "op", "op": op, "field": field_path,
                           "left": _safe(left), "right": _safe(value),
                           "result": False, "error": CHECK_FAILED}
        return result, {"kind": "op", "op": op, "field": field_path,
                        "left": _safe(left), "right": _safe(value), "result": result}
    raise TemplateInvalidError("Condition did not match any supported form")


# Structurally check a condition without needing a document.
def validate_condition(condition: Any, where: str = "condition") -> None:
    """Raise if *condition* is not a shape evaluate_condition can run.

    Publishing is the right place to catch a typo like ``"op": "gte "`` or a
    missing ``field``. Without this the template saves happily and the mistake
    only surfaces later, as a 422 in the middle of somebody's approval - at
    which point the workflow is already stuck. Registered ``fn`` keys are
    checked too, since an unregistered key is equally fatal at run time.
    """
    if condition in (None, {}):
        return
    if not isinstance(condition, dict):
        raise TemplateInvalidError(f"{where}: must be a JSON object or null.")

    for key in ("all", "any"):
        if key in condition:
            children = condition[key]
            if not isinstance(children, list) or not children:
                raise TemplateInvalidError(f"{where}: '{key}' must be a non-empty list.")
            for i, child in enumerate(children):
                validate_condition(child, f"{where}.{key}[{i}]")
            return
    if "not" in condition:
        validate_condition(condition["not"], f"{where}.not")
        return
    if "fn" in condition:
        key = condition["fn"]
        if not key or not isinstance(key, str):
            raise TemplateInvalidError(f"{where}: 'fn' must be a registered function key.")
        try:
            get_condition_function(key)
        except Exception as exc:
            raise TemplateInvalidError(f"{where}: {exc}") from exc
        args = condition.get("args")
        if args is not None and not isinstance(args, dict):
            raise TemplateInvalidError(f"{where}: 'args' must be an object.")
        return
    if "op" in condition:
        op = condition["op"]
        if op not in CONDITION_OPERATORS:
            raise TemplateInvalidError(
                f"{where}: unsupported operator '{op}'. "
                f"Allowed: {', '.join(sorted(CONDITION_OPERATORS))}.")
        if not condition.get("field"):
            raise TemplateInvalidError(f"{where}: 'field' is required.")
        if op in (CONDITION_OP_IN, CONDITION_OP_NOT_IN) and \
                not isinstance(condition.get("value"), (list, tuple)):
            raise TemplateInvalidError(f"{where}: '{op}' needs a list 'value'.")
        return
    raise TemplateInvalidError(
        f"{where}: expected one of 'all', 'any', 'not', 'fn', or 'op'.")
