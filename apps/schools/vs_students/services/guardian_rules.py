"""A school's own guardian rules: how many, whether an email, how matched, which relationships.

Four ``vs_config`` definitions, all school-scoped, read through
``rules.resolve_many`` so inheritance is vs_config's own. Each default is the
behaviour every school had before it could choose, so a school that has set
nothing is treated exactly as before:

* every child needs one guardian, and the last one on the roll cannot be
  removed;
* a guardian needs a phone number and no email;
* a guardian typed in is matched to one the school holds on email, then phone;
* a relationship is one of the eight fixed ones, and anything else in a
  spreadsheet is imported as Other.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: a minimum out of range reads
as 1, an unknown matching mode as EMAIL_THEN_PHONE, and the added relationships
are trimmed, deduplicated and stripped of anything that is not a label a
school could have saved. A bad stored value then costs a school its own rule,
never an enrolment.

Writes go through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail. A value already in force
is not written again, so saving the screen unchanged leaves no audit rows.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ..constants import (
    CFG_GUARDIAN_EMAIL_REQUIRED,
    CFG_GUARDIAN_EXTRA_RELATIONSHIPS,
    CFG_GUARDIAN_MATCHING,
    CFG_GUARDIAN_MINIMUM,
    EXTRA_RELATIONSHIP_MAX_LENGTH,
    EXTRA_RELATIONSHIPS_MAX,
    GUARDIAN_MINIMUM_CEILING,
    GUARDIAN_MINIMUM_FLOOR,
    GuardianMatching,
    Relationship,
)
from ..exceptions import StudentSettingNotRegistered
from .rules import resolve_many

_RULE_KEYS = (
    CFG_GUARDIAN_MINIMUM, CFG_GUARDIAN_EMAIL_REQUIRED, CFG_GUARDIAN_MATCHING,
    CFG_GUARDIAN_EXTRA_RELATIONSHIPS,
)

#: Every spelling of a fixed relationship a person or a spreadsheet uses: the
#: stored code and the label, folded. ``LEGAL_GUARDIAN`` and "Legal guardian"
#: are the one pair that differ.
FIXED_SPELLINGS: dict[str, str] = {}
for _code, _label in Relationship.choices:
    FIXED_SPELLINGS[_code.casefold()] = _code
    FIXED_SPELLINGS[_label.casefold()] = _code


def _minimum(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return GUARDIAN_MINIMUM_FLOOR
    if not GUARDIAN_MINIMUM_FLOOR <= value <= GUARDIAN_MINIMUM_CEILING:
        return GUARDIAN_MINIMUM_FLOOR
    return value


def _matching(value) -> str:
    if value in GuardianMatching.values:
        return str(value)
    return GuardianMatching.EMAIL_THEN_PHONE.value


def clean_extras(value) -> tuple:
    """The stored list, as labels a school could have saved, in its own order.

    Blank, overlong, repeated and fixed-relationship entries are dropped, and
    the list is cut at the most a school may hold.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    kept: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        label = raw.strip()
        folded = label.casefold()
        if not label or len(label) > EXTRA_RELATIONSHIP_MAX_LENGTH:
            continue
        if folded in seen or folded in FIXED_SPELLINGS:
            continue
        seen.add(folded)
        kept.append(label)
    return tuple(kept[:EXTRA_RELATIONSHIPS_MAX])


def resolve_relationship(raw, extras) -> tuple[str, str] | None:
    """``(code, detail)`` for a relationship as typed, or None when it is neither.

    A fixed relationship matches on its code or its label, ignoring case, and
    carries no detail. One of the school's *extras* matches ignoring case and
    is stored as OTHER with the label as the school spelled it in Settings,
    so "sponsor" typed in a spreadsheet is stored as the school's "Sponsor".
    """
    text = (raw or "").strip()
    if not text:
        return None
    folded = text.casefold()
    code = FIXED_SPELLINGS.get(folded)
    if code is not None:
        return code, ""
    for label in extras:
        if label.casefold() == folded:
            return Relationship.OTHER.value, label
    return None


def unknown_relationship_message(raw) -> str:
    """The refusal for a relationship that is blank, or neither fixed nor the school's."""
    text = (raw or "").strip()
    if not text:
        return "Say how this guardian is related to the child."
    return (
        f"'{text}' is not a relationship this school records. Pick one from "
        f"the list, or add it in Settings, Guardians."
    )


@dataclass(frozen=True)
class GuardianRules:
    min_per_student: int = GUARDIAN_MINIMUM_FLOOR
    email_required: bool = False
    matching: str = GuardianMatching.EMAIL_THEN_PHONE.value
    extra_relationships: tuple = ()

    @property
    def email_only(self) -> bool:
        return self.matching == GuardianMatching.EMAIL_ONLY

    def resolve_relationship(self, raw) -> tuple[str, str] | None:
        return resolve_relationship(raw, self.extra_relationships)

    def relationships(self) -> list[dict]:
        """The choices a relationship picker offers, OTHER last.

        The fixed relationships in their own order, then the school's, whose
        value is the label itself because that is what a write path accepts.
        """
        fixed = [
            {"value": code, "label": label}
            for code, label in Relationship.choices
            if code != Relationship.OTHER
        ]
        added = [{"value": label, "label": label} for label in self.extra_relationships]
        return [
            *fixed, *added,
            {"value": Relationship.OTHER.value, "label": Relationship.OTHER.label},
        ]

    def as_dict(self) -> dict:
        """The body of ``GET /v1/students/guardian-rules/``."""
        return {
            "min_per_student": self.min_per_student,
            "email_required": self.email_required,
            "matching": self.matching,
            "matching_options": [
                {"value": value, "label": label}
                for value, label in GuardianMatching.choices
            ],
            "extra_relationships": list(self.extra_relationships),
            "relationships": self.relationships(),
        }


def read_guardian_rules(tenant) -> GuardianRules:
    """All four rules in one read: five queries whatever the school.

    With no tenant, the defaults: a caller judging for no school is held to
    the behaviour every school has until it chooses otherwise.
    """
    if tenant is None:
        return GuardianRules()
    found = {
        key: value
        for key, (value, _) in resolve_many(_RULE_KEYS, tenant=tenant).items()
    }
    return GuardianRules(
        min_per_student=_minimum(found.get(CFG_GUARDIAN_MINIMUM, GUARDIAN_MINIMUM_FLOOR)),
        email_required=found.get(CFG_GUARDIAN_EMAIL_REQUIRED) is True,
        matching=_matching(found.get(CFG_GUARDIAN_MATCHING)),
        extra_relationships=clean_extras(found.get(CFG_GUARDIAN_EXTRA_RELATIONSHIPS, [])),
    )


@transaction.atomic
def write_guardian_rules(
    tenant, actor, *, min_per_student, email_required, matching,
    extra_relationships, reason="",
) -> GuardianRules:
    """Store the school's guardian rules. The caller has already validated them.

    The added relationships are stored trimmed, in the order the school gave
    them, so the picker offers them in that order.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value, set_value

    wanted = {
        CFG_GUARDIAN_MINIMUM: min_per_student,
        CFG_GUARDIAN_EMAIL_REQUIRED: bool(email_required),
        CFG_GUARDIAN_MATCHING: matching,
        CFG_GUARDIAN_EXTRA_RELATIONSHIPS: list(clean_extras(extra_relationships)),
    }
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise StudentSettingNotRegistered(key=key)

    why = (reason or "").strip() or "Guardian rules set from Student settings."
    for key, value in wanted.items():
        definition = definitions[key]
        current, _ = resolve_value(definition, tenant=tenant)
        if current == value:
            continue
        set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason=why,
        )
    return read_guardian_rules(tenant)
