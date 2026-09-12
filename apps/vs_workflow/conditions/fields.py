"""The fields a Dynamic Role condition may test, and how each may be compared.

A condition names a field by its path in the rule context (see
:mod:`vs_workflow.conditions.context`). Fields are grouped into **areas** - the
part of the school a condition asks about. ``document`` is the document itself,
``requester`` the person who raised it, and a domain app may declare an area of
its own: the child a bill is for, the member of staff a leave request belongs
to. A screen asks for the area first and then offers that area's fields, so
somebody building a rule picks "the student", then "their class", rather than
knowing that the path is ``student.class_name``.

Every field says which document types can answer it. A Dynamic Role is written
without naming a document type - it is picked on a stage, and the stage's
template is what fixes the type - so the editor offers the whole catalogue and
publishing refuses a rule the stage's own document cannot answer. A refund
knows its amount and its customer; it has no leave type, and a rule testing one
would never be true.

Four places contribute:

* the engine declares the shared fields - the amount, the branch, the document
  type - and the requester's id, branch and roles, which every tenant has;
* a document type declares its own fields on its workflow handler, as
  ``BaseWorkflowHandler.condition_fields``;
* a domain app declares requester facts only it can read with
  :func:`register_requester_field`;
* a domain app declares a whole area with :func:`register_area` and
  :func:`register_area_field`, naming the document types it can be reached
  from, and registers the resolver that reaches it
  (:func:`vs_workflow.conditions.context.register_area_resolver`). A school's
  roll lives in the school app, which the engine may not import.
"""
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Tuple

from vs_workflow.constants import (
    CONDITION_OP_CONTAINS, CONDITION_OP_EQ, CONDITION_OP_GT, CONDITION_OP_GTE,
    CONDITION_OP_IN, CONDITION_OP_LT, CONDITION_OP_LTE, CONDITION_OP_NE,
    CONDITION_OP_NOT_IN, ConditionFieldType,
)
from vs_workflow.exceptions import UnknownDocumentTypeError

AREA_DOCUMENT = "document"
AREA_REQUESTER = "requester"
#: The names these two areas were known by when a condition only had a subject.
SUBJECT_DOCUMENT = AREA_DOCUMENT
SUBJECT_REQUESTER = AREA_REQUESTER

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
class ConditionArea:
    """A part of the school a condition can ask about.

    Attributes:
        key: The first segment of its fields' paths, e.g. ``student``.
        label: What the screen calls it.
        document_types: The types whose documents reach it. Empty means every
            document does, as the document itself and its requester do.
        order: Where it sits in the picker; the engine's own areas come first.
    """

    key: str
    label: str
    document_types: Tuple[str, ...] = ()
    order: int = 100

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "document_types": list(self.document_types),
        }


@dataclass(frozen=True)
class ConditionField:
    """One thing a condition may test.

    Attributes:
        key: Its path in the rule context, e.g. ``amount`` or ``student.class_name``.
        label: What the screen calls it.
        area: The :class:`ConditionArea` it belongs to.
        type: A :class:`~vs_workflow.constants.ConditionFieldType`, which fixes
            its operators and the kind of value it compares.
        choices: ``(value, label)`` pairs, for a CHOICE field only.
        document_types: The types that can answer it. Empty means every type,
            and it is filled in by :func:`catalogue` for a field whose handler
            or area already says which documents reach it.
    """

    key: str
    label: str
    area: str
    type: str
    choices: Tuple[Tuple[str, str], ...] = ()
    document_types: Tuple[str, ...] = ()

    @property
    def operators(self) -> Tuple[str, ...]:
        return OPERATORS_BY_TYPE[self.type]

    def answers(self, document_type: str) -> bool:
        """Whether a document of *document_type* can answer this field."""
        return not self.document_types or document_type in self.document_types

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "area": self.area,
            # The name this carried when a condition had a subject rather than
            # an area. Kept so a screen on the older shape keeps working.
            "subject": self.area,
            "type": str(self.type),
            "operators": list(self.operators),
            "choices": [{"value": value, "label": label} for value, label in self.choices],
            "document_types": list(self.document_types),
        }


AMOUNT_FIELD = ConditionField("amount", "Amount", AREA_DOCUMENT, ConditionFieldType.MONEY)
BRANCH_FIELD = ConditionField("branch", "Branch", AREA_DOCUMENT, ConditionFieldType.BRANCH)
REQUESTER_FIELDS = (
    ConditionField("requester.id", "The person who raised it", AREA_REQUESTER,
                   ConditionFieldType.PERSON),
    ConditionField("requester.role_keys", "Their role", AREA_REQUESTER,
                   ConditionFieldType.ROLE),
    ConditionField("requester.branch", "Their branch", AREA_REQUESTER,
                   ConditionFieldType.BRANCH),
)

_REGISTERED_REQUESTER_FIELDS: List[ConditionField] = []
_AREAS: Dict[str, ConditionArea] = {}
_AREA_FIELDS: Dict[str, List[ConditionField]] = {}


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


def register_area(area: ConditionArea) -> ConditionArea:
    """Declare an area a domain app owns, such as the child a bill is for.

    The app also registers how the area is reached from a document
    (:func:`vs_workflow.conditions.context.register_area_resolver`); an area
    declared without one is simply never true. Registering the same key again,
    as an app reload does, changes nothing.
    """
    if area.key in (AREA_DOCUMENT, AREA_REQUESTER):
        raise ValueError(f"'{area.key}' is the engine's own area.")
    _AREAS.setdefault(area.key, area)
    _AREA_FIELDS.setdefault(area.key, [])
    return _AREAS[area.key]


def register_area_field(field: ConditionField) -> ConditionField:
    """Declare one field of a registered area. Its key starts with the area's."""
    area = _AREAS.get(field.area)
    if area is None:
        raise ValueError(f"Register the '{field.area}' area before its fields.")
    if not field.key.startswith(f"{field.area}."):
        raise ValueError(f"A {field.area} field's key starts with '{field.area}.', not '{field.key}'.")
    fields = _AREA_FIELDS[field.area]
    if all(existing.key != field.key for existing in fields):
        fields.append(field)
    return field


def registered_areas() -> List[ConditionArea]:
    """The areas apps have declared, in the order a screen should offer them."""
    return sorted(_AREAS.values(), key=lambda area: (area.order, area.label))


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


def _known_types(document_types: Iterable[str]) -> List[str]:
    """The types in scope: the ones asked for, or every registered type."""
    types = [t for t in dict.fromkeys(document_types or []) if t]
    if types:
        return types
    from vs_workflow.handlers.registry import list_registered_handlers

    return sorted(list_registered_handlers())


def areas_for(document_types: Iterable[str] = ()) -> List[ConditionArea]:
    """The areas worth offering for *document_types*, the engine's own first."""
    types = _known_types(document_types)
    offered = [
        ConditionArea(AREA_DOCUMENT, "This document", order=0),
        ConditionArea(AREA_REQUESTER, "The person who raised it", order=1),
    ]
    for area in registered_areas():
        if not area.document_types or any(t in area.document_types for t in types):
            offered.append(area)
    return offered


def catalogue(document_types: Iterable[str] = ()) -> List[ConditionField]:
    """Every field a condition may test, each saying which documents answer it.

    Called without document types - which is how a Dynamic Role is written,
    since it names none - it offers the whole system: the amount and branch
    every document has, each document type's own fields, the requester, and
    every area an app has declared. Called with types, it narrows to what those
    documents can answer, which is what a stage's publish check and a trial run
    use.

    ``document_type`` is offered only when there are several types to tell
    apart, because a rule cannot usefully compare a document type there is only
    one of.
    """
    types = _known_types(document_types)
    asked = [t for t in dict.fromkeys(document_types or []) if t]
    fields: List[ConditionField] = []

    if len(types) > 1:
        fields.append(ConditionField(
            "document_type", "Document type", AREA_DOCUMENT, ConditionFieldType.CHOICE,
            tuple((t, document_type_label(t)) for t in types)))
    with_amount = tuple(t for t in types if has_amount(t))
    if with_amount:
        fields.append(replace(AMOUNT_FIELD, document_types=with_amount))
    fields.append(BRANCH_FIELD)

    # A document type's own fields. Offered for one type as that type's, and
    # across several so the catalogue can show everything the system holds -
    # each one still saying which document answers it.
    for document_type in types:
        handler = _handler(document_type)
        for field in getattr(handler, "condition_fields", ()) or ():
            fields.append(replace(field, document_types=(document_type,)))

    fields.extend(REQUESTER_FIELDS)
    fields.extend(_REGISTERED_REQUESTER_FIELDS)
    for area in registered_areas():
        if asked and area.document_types and not any(t in area.document_types for t in asked):
            continue
        for field in _AREA_FIELDS.get(area.key, []):
            fields.append(replace(field, document_types=area.document_types))
    return fields


def fields_for(document_types: Iterable[str] = ()) -> List[ConditionField]:
    """:func:`catalogue`, kept under the name its callers already use."""
    return catalogue(document_types)


def field_map(document_types: Iterable[str] = ()) -> Dict[str, ConditionField]:
    """:func:`catalogue`, keyed by field path."""
    return {field.key: field for field in catalogue(document_types)}


def unanswerable(field_keys: Iterable[str], document_type: str) -> List[str]:
    """Which of *field_keys* a document of *document_type* cannot answer.

    A rule testing one of these could never be true, so a stage naming the
    Dynamic Role that holds it is refused at publish rather than resolving to
    nobody months later.
    """
    known = field_map()
    answerable = {key for key, field in known.items() if field.answers(document_type)}
    return [key for key in dict.fromkeys(field_keys) if key in known and key not in answerable]
