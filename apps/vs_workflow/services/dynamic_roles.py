"""Named Dynamic Roles: checking their rules, and working out who they choose.

A Dynamic Role is ordered rules - "when this, then them" - read top to bottom,
the first match deciding who approves and a final Otherwise row catching every
document the others do not. This module is the one place those rules are
checked and the one place they are run, so the rule the screen tries, the rule
the engine activates and the rule the audit records are the same rule.

Everything that would let a Dynamic Role reach nobody by mistake is refused
when its rules are saved rather than found out at approval time:

* a document type its tenant never raises, whose documents never arrive;
* a field the document type does not have, which would never be true;
* an operator the field cannot use, or a value of the wrong kind - an amount
  that is not whole kobo, a choice that is not on the list, a branch, role or
  person that is not the tenant's;
* anything but comparisons joined by "and" - no "or", "not" or custom
  function, since "or" is a second rule sending to the same place;
* a target that cannot approve: a role the engine will not nominate, or a
  person or group outside the tenant;
* a set with no Otherwise row, or with one anywhere but last.
"""
from typing import Iterable, List, Optional, Tuple

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from vs_workflow.conditions.evaluator import evaluate_condition
from vs_workflow.conditions.fields import ConditionField, field_map
from vs_workflow.constants import (
    CONDITION_OP_IN, CONDITION_OP_NOT_IN, ConditionFieldType, DynamicRoleTargetKind,
)
from vs_workflow.exceptions import TemplateInvalidError

_COMBINATORS = frozenset({"all", "any", "not", "fn"})


def _is_leaf(condition) -> bool:
    return (isinstance(condition, dict) and "op" in condition and "field" in condition
            and not (_COMBINATORS & set(condition)))


def _leaves(condition, where: str) -> list:
    """The comparisons in *condition*: one on its own, or several under ``all``."""
    if _is_leaf(condition):
        return [condition]
    if isinstance(condition, dict) and set(condition) == {"all"}:
        children = condition["all"]
        if isinstance(children, list) and children and all(_is_leaf(c) for c in children):
            return children
    raise TemplateInvalidError(
        f"{where}: a condition is comparisons joined by \"and\". For \"or\", add a "
        "second rule sending to the same place.")


def _tenant_user(tenant, user_id):
    """The active user *user_id* in *tenant*, or None, without saying which check failed."""
    from django.contrib.auth import get_user_model

    if user_id in (None, ""):
        return None
    try:
        return get_user_model().objects.filter(pk=user_id, tenant=tenant, is_active=True).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


def _check_value(field: ConditionField, value, tenant, where: str):
    """Refuse a value of the wrong kind for *field*, and return it normalised."""
    kind = field.type
    if kind in (ConditionFieldType.MONEY, ConditionFieldType.NUMBER):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TemplateInvalidError(f"{where}: {field.label} is compared with a number.")
        if kind == ConditionFieldType.MONEY:
            if value < 0 or not float(value).is_integer():
                raise TemplateInvalidError(
                    f"{where}: an amount is whole kobo and cannot be negative.")
            return int(value)
        return value
    if kind == ConditionFieldType.TEXT:
        if not isinstance(value, str) or not value.strip():
            raise TemplateInvalidError(f"{where}: {field.label} is compared with some text.")
        return value.strip()
    if kind == ConditionFieldType.CHOICE:
        if str(value) not in {choice for choice, _ in field.choices}:
            raise TemplateInvalidError(
                f"{where}: '{value}' is not one of the choices for {field.label}.")
        return str(value)
    if kind == ConditionFieldType.ROLE:
        from vs_rbac.models import TenantRoleTemplate

        if not TenantRoleTemplate.objects.filter(
                tenant=tenant, key=str(value),
                status=TenantRoleTemplate.Status.ACTIVE).exists():
            raise TemplateInvalidError(
                f"{where}: no active role with key '{value}' exists in this tenant.")
        return str(value)
    if kind == ConditionFieldType.BRANCH:
        from vs_tenants.references import find_branch_in_tenant

        if find_branch_in_tenant(tenant, str(value)) is None:
            raise TemplateInvalidError(f"{where}: that branch is not one of yours.")
        return str(value)
    if kind == ConditionFieldType.PERSON:
        if _tenant_user(tenant, value) is None:
            raise TemplateInvalidError(
                f"{where}: no active user with that id exists in your tenant.")
        return str(value)
    raise TemplateInvalidError(f"{where}: {field.label} cannot be tested.")


def _check_leaf(leaf: dict, fields, tenant, where: str) -> dict:
    key, op, value = leaf.get("field"), leaf.get("op"), leaf.get("value")
    field = fields.get(key)
    if field is None:
        raise TemplateInvalidError(f"{where}: '{key}' is not something this Dynamic Role can test.")
    if op not in field.operators:
        raise TemplateInvalidError(f"{where}: {field.label} cannot be compared with '{op}'.")
    if op in (CONDITION_OP_IN, CONDITION_OP_NOT_IN):
        if not isinstance(value, list) or not value:
            raise TemplateInvalidError(f"{where}: '{op}' needs a list of values.")
        value = [_check_value(field, item, tenant, where) for item in value]
    else:
        value = _check_value(field, value, tenant, where)
    return {"op": op, "field": key, "value": value}


def _resolve_target(raw: dict, tenant, where: str) -> dict:
    """Who *raw* sends to, checked against *tenant*."""
    kind = raw.get("target_kind") or ""
    target = {"target_kind": kind, "role_key": "", "role": None, "user": None, "group": None}
    if kind == DynamicRoleTargetKind.ROLE:
        from vs_rbac.models import TenantRoleTemplate

        key = raw.get("role_key") or ""
        role = TenantRoleTemplate.objects.filter(
            tenant=tenant, key=key, status=TenantRoleTemplate.Status.ACTIVE).first()
        if role is None:
            raise TemplateInvalidError(
                f"{where}: no active role with key '{key}' exists in this tenant.")
        # The engine nominates only approving roles (see
        # approvers._users_for_role_key), so any other role would save and
        # then find nobody when a document arrived.
        if not role.is_system_role:
            raise TemplateInvalidError(
                f"{where}: {role.name} is not a role that can approve. Pick an approving "
                "role, or put these people in an approver group.")
        return target | {"role_key": role.key, "role": role}
    if kind == DynamicRoleTargetKind.USER:
        user = _tenant_user(tenant, raw.get("user"))
        if user is None:
            raise TemplateInvalidError(
                f"{where}: no active user with that id exists in your tenant.")
        return target | {"user": user}
    if kind == DynamicRoleTargetKind.GROUP:
        from vs_workflow.models import WorkflowApproverGroup

        code = raw.get("group_code") or ""
        group = WorkflowApproverGroup.all_objects.filter(
            tenant=tenant, code=code, is_active=True).first()
        if group is None:
            raise TemplateInvalidError(
                f"{where}: no active approver group with code '{code}' exists in this tenant.")
        return target | {"group": group}
    raise TemplateInvalidError(f"{where}: send it to a role, a person or an approver group.")


def check_document_types(tenant, document_types: Iterable[str]) -> List[str]:
    """The document types a Dynamic Role of *tenant* may serve, without repeats.

    Refuses a type no handler owns, and a type *tenant* never raises. The
    second is the one a screen can walk into: a school offered platform user
    creation would save careful rules that no document ever reaches. The
    fields endpoint, the save and every trial run all come through here, so
    what one offers the others accept.
    """
    from vs_workflow.conditions.fields import document_type_label
    from vs_workflow.handlers.registry import list_registered_handlers, raises

    registered = list_registered_handlers()
    types = list(dict.fromkeys(t for t in (document_types or []) if t))
    for document_type in types:
        handler = registered.get(document_type)
        if handler is None:
            raise TemplateInvalidError(
                f"'{document_type}' is not a document type that can be approved.")
        if not raises(handler, tenant):
            raise TemplateInvalidError(
                f"{document_type_label(document_type)} is never raised here, so a "
                "Dynamic Role for it would never be used.")
    return types


def validate_rules(*, tenant, document_types: Iterable[str], rules) -> List[dict]:
    """Check a Dynamic Role's *rules* and resolve their targets.

    Returns one spec per rule, in evaluation order, ready for
    :func:`replace_rules`. Raises TemplateInvalidError naming the rule
    (counted from 1) and what is wrong with it.
    """
    document_types = check_document_types(tenant, document_types)
    if not isinstance(rules, list) or not rules:
        raise TemplateInvalidError("A Dynamic Role needs at least its Otherwise rule.")
    fields = field_map(document_types)
    last = len(rules) - 1
    specs = []
    for i, raw in enumerate(rules):
        where = f"Rule {i + 1}"
        if not isinstance(raw, dict):
            raise TemplateInvalidError(f"{where}: each rule must be an object.")
        condition = raw.get("condition")
        if condition in (None, {}):
            if i != last:
                raise TemplateInvalidError(
                    f"{where}: the Otherwise rule must be last - the {last - i} rule(s) "
                    "after it could never match.")
            condition = None
        elif i == last:
            raise TemplateInvalidError(
                f"{where}: the last rule is the Otherwise rule, with no condition, so "
                "every document reaches somebody.")
        else:
            leaves = [_check_leaf(leaf, fields, tenant, where)
                      for leaf in _leaves(condition, where)]
            condition = leaves[0] if len(leaves) == 1 else {"all": leaves}
        specs.append({
            "order": i,
            "condition": condition,
            **_resolve_target(raw, tenant, where),
            "label": str(raw.get("label") or "")[:150],
        })
    return specs


@transaction.atomic
def replace_rules(dynamic_role, specs: List[dict]) -> None:
    """Store *specs* as *dynamic_role*'s rules, replacing the ones it had."""
    from vs_workflow.models import WorkflowDynamicRoleRule

    dynamic_role.rules.all().delete()
    WorkflowDynamicRoleRule.objects.bulk_create([
        WorkflowDynamicRoleRule(dynamic_role=dynamic_role, **spec) for spec in specs
    ])


def rules_as_payload(dynamic_role) -> List[dict]:
    """A Dynamic Role's stored rules in the shape :func:`validate_rules` accepts."""
    return [
        {
            "condition": rule.condition,
            "target_kind": rule.target_kind,
            "role_key": rule.role_key,
            "user": str(rule.user_id) if rule.user_id else "",
            "group_code": rule.group.code if rule.group_id else "",
            "label": rule.label,
        }
        for rule in dynamic_role.rules.select_related("group").all()
    ]


def describe_target(rule) -> Optional[dict]:
    """Who a rule sends to, in a form the screen and the audit can both show."""
    if rule is None:
        return None
    if rule.target_kind == DynamicRoleTargetKind.USER:
        user = rule.user
        name = (getattr(user, "full_name", "") or user.get_username()) if user else ""
        return {"kind": rule.target_kind, "id": str(rule.user_id or ""), "name": name}
    if rule.target_kind == DynamicRoleTargetKind.GROUP:
        group = rule.group
        return {"kind": rule.target_kind, "code": group.code if group else "",
                "name": group.name if group else ""}
    role = rule.role
    return {"kind": rule.target_kind, "key": rule.role_key,
            "name": role.name if role else rule.role_key}


def match_rule(rules: Iterable, context) -> Tuple[Optional[object], List[dict]]:
    """The first of *rules* whose condition holds in *context*, with the trace.

    Evaluation stops at the match, so the trace lists the rules tried and the
    one that fired - which is what "why did this go to the Bursar" needs.
    """
    evaluations, chosen = [], None
    for rule in rules:
        matched, trace = evaluate_condition(rule.condition, context)
        evaluations.append({
            "rule_id": None if rule._state.adding else str(rule.pk),
            "order": rule.order,
            "target": describe_target(rule),
            "is_fallback": rule.is_fallback,
            "trace": trace,
            "picked": bool(matched),
        })
        if matched:
            chosen = rule
            break
    return chosen, evaluations


def rule_users(rule, tenant, branch) -> list:
    """The people *rule* sends to, before containment and self-approval.

    *branch* narrows a role's holders the way a BRANCH-scoped stage does, and
    narrows an approver group's role members the same way; a named person is
    who they are wherever the document came from.
    """
    from vs_workflow.services.approvers import _users_for_role_key, resolve_group_users

    if rule is None:
        return []
    if rule.target_kind == DynamicRoleTargetKind.USER:
        return [rule.user] if rule.user_id else []
    if rule.target_kind == DynamicRoleTargetKind.GROUP:
        return resolve_group_users(rule.group, tenant, branch)
    return _users_for_role_key(rule.role_key, tenant, branch)


def preview(*, tenant, requester, document_types, rules, sample=None, branch=None):
    """Try unsaved *rules* for *requester* and a sample document.

    Runs the same checks as saving and the same matching as activation, then
    contains the answer to the tenant and drops the requester, so the screen's
    answer is the engine's. Returns ``(users, detail)``.
    """
    from vs_workflow.conditions.context import build_sample_context
    from vs_workflow.models import WorkflowDynamicRoleRule
    from vs_workflow.services.approvers import _tenant_members

    types = list(document_types or [])
    specs = validate_rules(tenant=tenant, document_types=types, rules=rules)
    candidates = [WorkflowDynamicRoleRule(**spec) for spec in specs]
    context = build_sample_context(
        requester=requester, tenant=tenant,
        document_type=types[0] if len(types) == 1 else "", sample=sample)
    chosen, evaluations = match_rule(candidates, context)
    users = _tenant_members(rule_users(chosen, tenant, branch), tenant.pk)
    users = [user for user in users if user.pk != requester.pk]
    return users, {
        "matched_order": chosen.order if chosen else None,
        "matched_target": describe_target(chosen),
        "evaluations": evaluations,
    }
