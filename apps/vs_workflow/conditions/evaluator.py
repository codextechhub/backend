"""JSON condition evaluator with trace output."""
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

def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    return value

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

def _safe(v: Any):
    if isinstance(v, Decimal): return str(v)
    try:
        import json; json.dumps(v); return v
    except (TypeError, ValueError): return str(v)

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
        except Exception as exc:
            return False, {"kind": "fn", "fn": key, "args": args, "result": False,
                           "error": f"{type(exc).__name__}: {exc}"}
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
        left = None if extracted is _MISSING else extracted
        try:
            result = _apply_op(op, left, value)
        except TypeError as exc:
            return False, {"kind": "op", "op": op, "field": field_path,
                           "left": _safe(left), "right": _safe(value),
                           "result": False, "error": f"{type(exc).__name__}: {exc}"}
        return result, {"kind": "op", "op": op, "field": field_path,
                        "left": _safe(left), "right": _safe(value), "result": result}
    raise TemplateInvalidError("Condition did not match any supported form")
