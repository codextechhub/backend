"""A school's academic structure settings: its word for a term, its term names, its arms.

Three ``vs_config`` definitions, all school-scoped, read through
``resolve_value`` so a school's value, the platform's and the definition's
default are inherited exactly as every other setting is:

* ``academics.terms.word``: TERM or SEMESTER, the word every backend sentence
  about a term uses (``words.term_word``);
* ``academics.terms.names``: the ordered names a new academic year's terms are
  given, one to six of them. The count is the school's too: a school that
  names four terms runs four;
* ``academics.classes.default_arms``: the arms "generate arms" makes, one to
  twelve of them. A class is always named "{level} {arm}".

**Two defaults come from the school's term structure**, the choice a school
makes at onboarding and cannot change once it is live. Their definitions
default to null, and a null reads as that structure's answer: a school on
``2_SEMESTERS`` says Semester and names First Semester and Second Semester; one
on ``3_TERMS`` says Term and names First, Second and Third Term. A school that
has set nothing therefore reads what its onboarding choice implied, and a value
the school saves replaces it. The default arms are A, B and C whatever the
structure.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: a list that breaks a rule
the screen enforces reads as the default. A bad stored value then costs a
school its own choice, never a screen.

Writes go through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail. A value already in force
is not written again, so saving the screen unchanged leaves no audit rows, and
a school that saved the defaults its structure implied keeps following that
structure if it corrects the structure before going live. Changing the word
never renames a term that already exists: stored names are the school's own
text.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ..constants import (
    CFG_DEFAULT_ARMS,
    CFG_TERM_NAMES,
    CFG_TERM_WORD,
    DEFAULT_ARMS,
    DEFAULT_ARMS_MAX,
    NAME_MAX_LENGTH,
    TERM_NAMES_MAX,
    TermWord,
)
from ..exceptions import AcademicSettingNotRegistered

_RULE_KEYS = (CFG_TERM_WORD, CFG_TERM_NAMES, CFG_DEFAULT_ARMS)

#: The names each term structure implies, in order.
_NAMES_BY_WORD = {
    TermWord.TERM.value: ("First Term", "Second Term", "Third Term"),
    TermWord.SEMESTER.value: ("First Semester", "Second Semester"),
}


def resolve_many(keys, *, tenant):
    """``{key: value}`` for each active definition among *keys*.

    One query for the definitions and one per key through ``resolve_value``,
    so the inheritance order is vs_config's own rather than a copy of it. A key
    with no active definition is absent from the answer.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value

    definitions = ConfigurationDefinition.objects.filter(
        key__in=list(keys), is_active=True,
    )
    return {
        definition.key: resolve_value(definition, tenant=tenant)[0]
        for definition in definitions
    }


def structure_word(tenant) -> str:
    """The word the school's term structure implies: SEMESTER for two semesters, else TERM."""
    from schools.vs_schools.models import School, TermStructure

    structure = (
        School.objects.filter(tenant=tenant)
        .values_list("term_structure", flat=True).first()
    )
    if structure == TermStructure.TWO_SEMESTERS:
        return TermWord.SEMESTER.value
    return TermWord.TERM.value


def name_list_problem(value, *, most: int) -> tuple[str, str] | None:
    """Why *value* is not a usable list of names, as ``(cause, the entry)``, or None.

    The one rule both lists keep: 1 to *most* entries, each text, non-blank,
    at most ``NAME_MAX_LENGTH`` characters, and none repeated whatever its
    case. The cause is ``empty``, ``too_many``, ``blank``, ``too_long`` or
    ``duplicate``; the entry is the one at fault, stripped, where there is
    one. The serializer words the refusal; the reader uses the answer to
    decide whether a stored value can be trusted.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return "empty", ""
    if len(value) > most:
        return "too_many", ""
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return "blank", ""
        name = item.strip()
        if len(name) > NAME_MAX_LENGTH:
            return "too_long", name
        if name.casefold() in seen:
            return "duplicate", name
        seen.add(name.casefold())
    return None


def _names(value, fallback, most) -> tuple[str, ...]:
    if value is None or name_list_problem(value, most=most) is not None:
        return tuple(fallback)
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class AcademicRules:
    term_word: str = TermWord.TERM.value
    term_names: tuple = _NAMES_BY_WORD[TermWord.TERM.value]
    default_arms: tuple = DEFAULT_ARMS

    def as_dict(self) -> dict:
        """The body of ``GET /v1/academics/rules/``.

        ``term_word_options`` are the choices the screen offers, each with the
        label it prints, so the screen carries no copy of them.
        """
        return {
            "term_word": self.term_word,
            "term_word_options": [
                {"value": value, "label": label} for value, label in TermWord.choices
            ],
            "term_names": list(self.term_names),
            "default_arms": list(self.default_arms),
        }


def read_academic_rules(tenant) -> AcademicRules:
    """The three settings in one read.

    Four queries, and a fifth for the term structure only when the word or
    the names fall back to it. With no tenant, a three-term school's defaults.
    """
    if tenant is None:
        return AcademicRules()
    found = resolve_many(_RULE_KEYS, tenant=tenant)
    stored_word = found.get(CFG_TERM_WORD)
    stored_names = found.get(CFG_TERM_NAMES)
    implied = None
    if stored_word not in TermWord.values or (
        stored_names is None
        or name_list_problem(stored_names, most=TERM_NAMES_MAX) is not None
    ):
        implied = structure_word(tenant)
    word = stored_word if stored_word in TermWord.values else implied
    return AcademicRules(
        term_word=word,
        term_names=_names(
            stored_names, _NAMES_BY_WORD[implied or TermWord.TERM.value],
            TERM_NAMES_MAX,
        ),
        default_arms=_names(
            found.get(CFG_DEFAULT_ARMS), DEFAULT_ARMS, DEFAULT_ARMS_MAX,
        ),
    )


def read_term_word(tenant) -> str:
    """TERM or SEMESTER alone, in two queries, or three when it falls back."""
    if tenant is None:
        return TermWord.TERM.value
    stored = resolve_many((CFG_TERM_WORD,), tenant=tenant).get(CFG_TERM_WORD)
    return stored if stored in TermWord.values else structure_word(tenant)


@transaction.atomic
def write_academic_rules(
    tenant, actor, *, term_word, term_names, default_arms, reason="",
) -> AcademicRules:
    """Store the school's academic structure settings. The caller has already validated them.

    Refused with ``ACADEMIC_SETTING_NOT_REGISTERED`` before anything is
    written when a definition is missing, so a half-saved set never exists.
    Each value is compared with what the school reads now, defaults included,
    so saving what the screen showed writes nothing.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import set_value

    wanted = {
        CFG_TERM_WORD: term_word,
        CFG_TERM_NAMES: [name.strip() for name in term_names],
        CFG_DEFAULT_ARMS: [arm.strip() for arm in default_arms],
    }
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise AcademicSettingNotRegistered(key=key)

    current = read_academic_rules(tenant)
    in_force = {
        CFG_TERM_WORD: current.term_word,
        CFG_TERM_NAMES: list(current.term_names),
        CFG_DEFAULT_ARMS: list(current.default_arms),
    }
    why = (reason or "").strip() or "Academic structure set from School settings."
    for key, value in wanted.items():
        if in_force[key] == value:
            continue
        set_value(
            definition=definitions[key], value=value, actor=actor, tenant=tenant,
            reason=why,
        )
    return read_academic_rules(tenant)
