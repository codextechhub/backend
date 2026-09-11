"""The fields a Dynamic Role condition may test, and how each may be compared.

A condition names a field by its path in the rule context (see
:mod:`vs_workflow.conditions.context`). ``amount``, ``branch`` and
``document_type`` exist for every document, ``requester.*`` describes the
person who raised it, and ``document.*`` reaches one document type's own
fields. The screen offers only what this module lists and the rule service
refuses anything else, so a field name that matches nothing - and with it a
rule that silently never fires - can never be saved.

Three places contribute:

* the engine declares the shared fields and the requester's id, branch and
  roles, which every tenant has;
* a document type declares its own fields on its workflow handler, as
  ``BaseWorkflowHandler.condition_fields``;
* a domain app declares requester facts only it can read with
  :func:`register_requester_field` - a school's job titles live in the school
  app, which the engine may not import.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from vs_workflow.constants import (
    CONDITION_OP_CONTAINS, CONDITION_OP_EQ, CONDITION_OP_GT, CONDITION_OP_GTE,
    CONDITION_OP_IN, CONDITION_OP_LT, CONDITION_OP_LTE, CONDITION_OP_NE,
    CONDITION_OP_NOT_IN, ConditionFieldType,
)
from vs_workflow.exceptions import UnknownDocumentTypeError

SUBJECT_DOCUMENT = "document"
SUBJECT_REQUESTER = "requester"

_ORDERED = (CONDITION_OP_GT, CONDITION_OP_GTE, CONDITION_OP_LT, CONDITION_OP_LTE,
            CONDITION_OP_EQ, CONDITION_OP_NE)
_ONE_OF = (CONDITION_OP_EQ, CONDITION_OP_NE, CONDITION_OP_IN, CONDITION_OP_NOT_IN)

#: The operators each kind of field may use. A requester holds a set of roles,
#: so a role is tested with ``contains`` - "has the role" - and nothing else.
OPERATORS_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    ConditionFieldType.MONEY: _ORDERED,
    ConditionFieldType.NUMBER: _ORDERED,
    ConditionFieldType.TEXT: (CONDITION_OP_EQ, CONDITION_OP_NE, CONDITION_OP_CONTAINS),
    ConditionFieldType.CHOICE: _ONE_OF,
    ConditionFieldType.BRANCH: _ONE_OF,
    ConditionFieldType.PERSON: _ONE_OF,
    ConditionFieldType.ROLE: (CONDITION_OP_CONTAINS,),
}


@dataclass(frozen=True)
class ConditionField:
    """One thing a condition may test.

    Attributes:
        key: Its path in the rule context, e.g. ``amount`` or ``document.days``.
        label: What the screen calls it.
        subject: ``document`` or ``requester`` - which question it answers.
        type: A :class:`~vs_workflow.constants.ConditionFieldType`, which fixes
            its operators and the kind of value it compares.
        choices: ``(value, label)`` pairs, for a CHOICE field only.
    """

    key: str
    label: str
    subject: str
    type: str
    choices: Tuple[Tuple[str, str], ...] = ()

    @property
    def operators(self) -> Tuple[str, ...]:
        return OPERATORS_BY_TYPE[self.type]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "subject": self.subject,
            "type": str(self.type),
            "operators": list(self.operators),
            "choices": [{"value": value, "label": label} for value, label in self.choices],
        }


AMOUNT_FIELD = ConditionField("amount", "Amount", SUBJECT_DOCUMENT, ConditionFieldType.MONEY)
BRANCH_FIELD = ConditionField("branch", "Branch", SUBJECT_DOCUMENT, ConditionFieldType.BRANCH)
REQUESTER_FIELDS = (
    ConditionField("requester.id", "The person who raised it", SUBJECT_REQUESTER,
                   ConditionFieldType.PERSON),
    ConditionField("requester.role_keys", "Their role", SUBJECT_REQUESTER,
                   ConditionFieldType.ROLE),
    ConditionField("requester.branch", "Their branch", SUBJECT_REQUESTER,
                   ConditionFieldType.BRANCH),
)

_REGISTERED_REQUESTER_FIELDS: List[ConditionField] = []


def register_requester_field(field: ConditionField) -> ConditionField:
    """Declare a requester fact a domain app supplies.

    The app also registers the provider that reads it (see
    :func:`vs_workflow.conditions.context.register_requester_facts`); a field
    declared without one is simply never true. Registering the same key again,
    as an app reload does, changes nothing.
    """
    if not field.key.startswith("requester."):
        raise ValueError(f"A requester field's key starts with 'requester.', not '{field.key}'.")
    if all(existing.key != field.key for existing in _REGISTERED_REQUESTER_FIELDS):
        _REGISTERED_REQUESTER_FIELDS.append(field)
    return field


def _handler(document_type: str):
    from vs_workflow.handlers import get_handler

    try:
        return get_handler(document_type)
    except UnknownDocumentTypeError:
        return None


def document_type_label(document_type: str) -> str:
    """What the screen calls a document type: its handler's noun, or its code in words."""
    handler = _handler(document_type)
    noun = getattr(handler, "noun", "") if handler is not None else ""
    if noun and noun != "Document":
        return noun
    return document_type.rsplit(".", 1)[-1].replace("_", " ").capitalize()


def has_amount(document_type: str) -> bool:
    """Whether documents of this type carry the amount ``amount`` reads."""
    handler = _handler(document_type)
    model = getattr(handler, "document_model", None) if handler is not None else None
    return bool(model is not None and getattr(model, "workflow_amount_field", ""))


def fields_for(document_types: Iterable[str]) -> List[ConditionField]:
    """Every field a Dynamic Role serving *document_types* may test.

    Serving no particular type means serving any, so only what every document
    has is offered: its branch and the requester. ``Amount`` joins when every
    served type carries one, and ``Document type`` when there are several to
    tell apart. A type's own fields are offered only when the role serves that
    type alone, because a field one type has is missing - and so never true -
    on every other.
    """
    types = [t for t in dict.fromkeys(document_types or []) if t]
    fields: List[ConditionField] = []
    if len(types) > 1:
        fields.append(ConditionField(
            "document_type", "Document type", SUBJECT_DOCUMENT, ConditionFieldType.CHOICE,
            tuple((t, document_type_label(t)) for t in types)))
    if types and all(has_amount(t) for t in types):
        fields.append(AMOUNT_FIELD)
    fields.append(BRANCH_FIELD)
    if len(types) == 1:
        handler = _handler(types[0])
        fields.extend(getattr(handler, "condition_fields", ()) or ())
    fields.extend(REQUESTER_FIELDS)
    fields.extend(_REGISTERED_REQUESTER_FIELDS)
    return fields


def field_map(document_types: Iterable[str]) -> Dict[str, ConditionField]:
    """:func:`fields_for`, keyed by field path."""
    return {field.key: field for field in fields_for(document_types)}
