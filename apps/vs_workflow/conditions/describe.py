"""A stored condition as a sentence a school administrator reads.

A condition is stored for the evaluator: ``{"op": "gte", "field": "total",
"value": 50000000}``, ``{"fn": "has_open_fees"}``, a field path, an operator
key and a value in kobo. Shown as it is stored, it reads as code. Every screen
that shows one shows :func:`describe_condition` instead: "Amount is at least
₦500,000.00", "Their branch is Ikeja Branch", "Petty cash return kind is
Closure or Amount is more than ₦10,000.00".

Two kinds of condition are stored, and they read different things:

* a **stage's own** condition (a stage's inclusion condition, a route, a
  stage's inline Dynamic Role rule) reads the document itself, so its path is
  the model's attribute: ``total``, ``kind``, ``shortage``. Pass the
  template's ``document_type`` and ``reads_document=True``. The path names the
  catalogue field ``document.<path>`` its handler declares, and the handler's
  money column (``workflow_amount_field``) is the amount;
* a **Dynamic Role** rule reads the rule context
  (:mod:`vs_workflow.conditions.context`), so its path is the catalogue key:
  ``amount``, ``requester.branch``, ``student.class_name``.

Labels, kinds and choices come from the catalogue
(:mod:`vs_workflow.conditions.fields`), and the names of the branches, people
and roles a condition holds from :class:`ConditionNames`, read in the reader's
tenant. Whatever cannot be named reads as a neutral phrase ("A detail of the
document", "a branch no longer on record", "A custom check"), never as the
path, the key or the id.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from vs_workflow.constants import (
    CONDITION_OP_CONTAINS, CONDITION_OP_EQ, CONDITION_OP_GT, CONDITION_OP_GTE,
    CONDITION_OP_IN, CONDITION_OP_LT, CONDITION_OP_LTE, CONDITION_OP_NE,
    CONDITION_OP_NOT_IN, ConditionFieldType,
)

#: How a missing condition reads: the stage or route always applies.
ALWAYS = "Always"
#: A path no catalogue field answers.
UNKNOWN_FIELD = "A detail of the document"
#: A choice value no longer among the field's choices.
RETIRED_CHOICE = "an option no longer offered"

_ORDERED = {
    CONDITION_OP_GT: "is more than",
    CONDITION_OP_GTE: "is at least",
    CONDITION_OP_LT: "is less than",
    CONDITION_OP_LTE: "is at most",
    CONDITION_OP_EQ: "is",
    CONDITION_OP_NE: "is not",
}
_ONE_OF = {
    CONDITION_OP_EQ: "is",
    CONDITION_OP_NE: "is not",
    CONDITION_OP_IN: "is one of",
    CONDITION_OP_NOT_IN: "is not one of",
}
_ANY = {**_ORDERED, **_ONE_OF, CONDITION_OP_CONTAINS: "contains"}
_OPERATOR_WORDS = {
    ConditionFieldType.MONEY: _ORDERED,
    ConditionFieldType.NUMBER: _ORDERED,
    ConditionFieldType.TEXT: {CONDITION_OP_EQ: "is", CONDITION_OP_NE: "is not",
                              CONDITION_OP_CONTAINS: "contains"},
    ConditionFieldType.CHOICE: _ONE_OF,
    ConditionFieldType.BRANCH: _ONE_OF,
    ConditionFieldType.PERSON: _ONE_OF,
    ConditionFieldType.ROLE: {CONDITION_OP_CONTAINS: "includes"},
}


def naira(kobo) -> str:
    """Whole kobo as naira: ``50000000`` reads ``₦500,000.00``."""
    try:
        amount = Decimal(str(kobo)) / 100
    except Exception:  # noqa: BLE001 - an unreadable amount still reads as words
        return "an amount"
    return f"₦{amount:,.2f}"


class ConditionNames:
    """The names of the branches, people and roles conditions hold, in one tenant.

    Each kind is read at most once per instance: branches and roles whole (a
    tenant holds few), people one id at a time and remembered. Pass one
    instance through every condition on a page, so a template with ten stages
    does not look the same branch up ten times.
    """

    def __init__(self, tenant):
        self.tenant = tenant
        self._branches: Optional[Dict[str, str]] = None
        self._roles: Optional[Dict[str, str]] = None
        self._people: Dict[str, Optional[str]] = {}

    def branch(self, value) -> str:
        if self._branches is None:
            from vs_tenants.models import Branch

            rows = Branch.objects.filter(tenant=self.tenant) if self.tenant else Branch.objects.none()
            self._branches = {str(pk): name for pk, name in rows.values_list("pk", "name")}
        return self._branches.get(str(value)) or "a branch no longer on record"

    def role_name(self, key) -> Optional[str]:
        """The name of the reader's role with *key*, or None."""
        if self._roles is None:
            self._roles = role_names(self.tenant)
        return self._roles.get(str(key))

    def role(self, key) -> str:
        return self.role_name(key) or "a role no longer defined"

    def person(self, value) -> str:
        key = str(value)
        if key not in self._people:
            from django.contrib.auth import get_user_model

            user = None
            if key.isdigit() and self.tenant is not None:
                user = get_user_model().objects.filter(pk=int(key), tenant=self.tenant).first()
            self._people[key] = (
                (getattr(user, "full_name", "") or user.get_username()) if user else None
            )
        return self._people[key] or "a person no longer on record"


def role_names(tenant) -> Dict[str, str]:
    """Role key to name for *tenant*'s roles; empty for no tenant."""
    from vs_rbac.models import TenantRoleTemplate

    names: Dict[str, str] = {}
    if tenant is not None:
        names.update(
            TenantRoleTemplate.objects.filter(tenant=tenant).values_list("key", "name")
        )
    return names


class ConditionDescriber:
    """Describes the conditions of one document type for one reader.

    Holds the catalogue for the document type and a :class:`ConditionNames`,
    so describing every condition on a template costs one catalogue build and a
    handful of name lookups.
    """

    def __init__(self, *, document_type: str = "", names: Optional[ConditionNames] = None,
                 tenant=None):
        from vs_workflow.conditions.fields import AMOUNT_FIELD, field_map

        self.document_type = document_type or ""
        self.names = names or ConditionNames(tenant)
        types: Iterable[str] = [self.document_type] if self.document_type else ()
        self.fields = field_map(types)
        self.fields.setdefault(AMOUNT_FIELD.key, AMOUNT_FIELD)
        self.amount_attribute = _amount_attribute(self.document_type)

    def field(self, path: str, *, reads_document: bool):
        """The catalogue field a stored path names, or None."""
        if reads_document:
            if path and path == self.amount_attribute:
                return self.fields.get("amount")
            own = self.fields.get(f"document.{path}")
            if own is not None:
                return own
        return self.fields.get(path)

    def describe(self, condition: Any, *, reads_document: bool = False) -> str:
        """*condition* as one sentence, capitalised; "Always" when there is none."""
        text = self._phrase(condition, reads_document=reads_document, nested=False)
        return text[:1].upper() + text[1:] if text else ALWAYS

    def _phrase(self, condition, *, reads_document, nested) -> str:
        from vs_workflow.conditions.registry import describe_condition_function

        if condition in (None, {}):
            return ""
        if not isinstance(condition, dict):
            return "a condition that cannot be read"
        for key, joiner in (("all", " and "), ("any", " or ")):
            if key in condition:
                parts = [
                    self._phrase(child, reads_document=reads_document, nested=True)
                    for child in condition.get(key) or []
                ]
                text = joiner.join(part for part in parts if part)
                return f"({text})" if nested and len(parts) > 1 else text
        if "not" in condition:
            inner = self._phrase(condition["not"], reads_document=reads_document, nested=True)
            return f"not ({inner})" if inner else ""
        if "fn" in condition:
            text = describe_condition_function(condition.get("fn") or "", condition.get("args"))
            return text[:1].lower() + text[1:] if nested else text
        if "op" in condition:
            return self._comparison(condition, reads_document=reads_document)
        return "a condition that cannot be read"

    def _comparison(self, condition, *, reads_document) -> str:
        op = condition.get("op")
        field = self.field(condition.get("field") or "", reads_document=reads_document)
        label = field.label if field is not None else UNKNOWN_FIELD
        kind = field.type if field is not None else None
        words = (_OPERATOR_WORDS.get(kind) or {}).get(op) or _ANY.get(op) or "matches"
        value = condition.get("value")
        if isinstance(value, (list, tuple)):
            values = [self._value(field, item) for item in value]
            shown = ", ".join(values[:-1]) + f" or {values[-1]}" if len(values) > 1 else (
                values[0] if values else "nothing")
        else:
            shown = self._value(field, value)
        return f"{label} {words} {shown}"

    def _value(self, field, value) -> str:
        if value is None or value == "":
            return "blank"
        if isinstance(value, bool):
            return "yes" if value else "no"
        kind = field.type if field is not None else None
        if kind == ConditionFieldType.MONEY:
            return naira(value)
        if kind == ConditionFieldType.NUMBER:
            try:
                return f"{Decimal(str(value)).normalize():,f}"
            except Exception:  # noqa: BLE001
                return "a number"
        if kind == ConditionFieldType.CHOICE:
            return dict(field.choices).get(str(value)) or RETIRED_CHOICE
        if kind == ConditionFieldType.BRANCH:
            return self.names.branch(value)
        if kind == ConditionFieldType.PERSON:
            return self.names.person(value)
        if kind == ConditionFieldType.ROLE:
            return self.names.role(value)
        if kind == ConditionFieldType.TEXT:
            return f"“{value}”"
        if isinstance(value, (int, float, Decimal)):
            return f"{value:,}"
        return "a set value"


def _amount_attribute(document_type: str) -> str:
    """The model attribute holding a document type's amount, or ``""``."""
    if not document_type:
        return ""
    from vs_workflow.conditions.fields import _handler

    handler = _handler(document_type)
    model = getattr(handler, "document_model", None) if handler is not None else None
    return getattr(model, "workflow_amount_field", "") or "" if model is not None else ""


def describe_condition(condition: Any, *, document_type: str = "", tenant=None,
                       reads_document: bool = False) -> str:
    """*condition* in words; see the module docstring for ``reads_document``."""
    return ConditionDescriber(document_type=document_type, tenant=tenant).describe(
        condition, reads_document=reads_document)
