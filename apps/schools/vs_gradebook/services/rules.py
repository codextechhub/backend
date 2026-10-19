"""A school's gradebook settings.

Seven ``vs_config`` definitions, all school-scoped, read through ``resolve_many``
so a school's value, the platform's and the definition's default are inherited
exactly as every other setting is:

* ``gradebook.show_class_stats``: whether a report card prints the class
  average, highest and lowest beside each subject. Default on.
* ``gradebook.rank_in_class``: whether the school ranks its students. When off,
  no position is shown anywhere. Default on.
* ``gradebook.conduct_ratings``: whether the class teacher rates conduct and
  skills. Default on.
* ``gradebook.rating_key``: whether a report card prints the line explaining the
  1 to 5 ratings. Default on.
* ``gradebook.rating_traits``: the traits a class teacher rates, in print order.
  One to fifteen distinct names of up to 60 characters. Defaults to ten.
* ``gradebook.moderated_programs``: the codes of the programmes whose sheets go
  to the head of the subject's department before they count. Empty by default,
  which means nothing is moderated.
* ``gradebook.results_tell_every_parent``: whether a released result is told to
  every parent of the child who has a parent account, in the parent app and on
  their phone, as well as to the primary guardian. Default off.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: a value that breaks a rule
the settings screen enforces reads as the default. A bad stored value then
costs a school its own choice, never a report card.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    CFG_CONDUCT_RATINGS,
    CFG_MODERATED_PROGRAMS,
    CFG_RANK_IN_CLASS,
    CFG_RATING_KEY,
    CFG_RATING_TRAITS,
    CFG_SHOW_CLASS_STATS,
    CFG_TELL_EVERY_PARENT,
    DEFAULT_CONDUCT_RATINGS,
    DEFAULT_RANK_IN_CLASS,
    DEFAULT_RATING_KEY,
    DEFAULT_RATING_TRAITS,
    DEFAULT_SHOW_CLASS_STATS,
    DEFAULT_TELL_EVERY_PARENT,
    RATING_TRAIT_NAME_MAX,
    RATING_TRAITS_MAX,
    RATING_TRAITS_MIN,
)

RULE_KEYS = (
    CFG_SHOW_CLASS_STATS,
    CFG_RANK_IN_CLASS,
    CFG_CONDUCT_RATINGS,
    CFG_RATING_KEY,
    CFG_RATING_TRAITS,
    CFG_MODERATED_PROGRAMS,
    CFG_TELL_EVERY_PARENT,
)


def clean_traits(value):
    """The trait list if it obeys the settings screen's rules, else None."""
    if not isinstance(value, (list, tuple)):
        return None
    if not RATING_TRAITS_MIN <= len(value) <= RATING_TRAITS_MAX:
        return None
    out = []
    for item in value:
        if not isinstance(item, str):
            return None
        name = item.strip()
        if not name or len(name) > RATING_TRAIT_NAME_MAX:
            return None
        if name.lower() in {seen.lower() for seen in out}:
            return None
        out.append(name)
    return tuple(out)


def clean_program_codes(value) -> tuple:
    """The programme codes, trimmed and without repeats; () when the value is not a list of codes."""
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return ()
        if item.strip().lower() not in {seen.lower() for seen in out}:
            out.append(item.strip())
    return tuple(out)


def _flag(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


@dataclass(frozen=True)
class GradebookRules:
    """The seven settings, cleaned, with every default a school starts on."""

    show_class_stats: bool = DEFAULT_SHOW_CLASS_STATS
    rank_in_class: bool = DEFAULT_RANK_IN_CLASS
    conduct_ratings: bool = DEFAULT_CONDUCT_RATINGS
    rating_key: bool = DEFAULT_RATING_KEY
    rating_traits: tuple = DEFAULT_RATING_TRAITS
    moderated_programs: tuple = ()
    tell_every_parent: bool = DEFAULT_TELL_EVERY_PARENT

    def moderates(self, program) -> bool:
        """Whether sheets of *program* (a programme or its code) go to moderation."""
        code = getattr(program, "code", program)
        return str(code).lower() in {c.lower() for c in self.moderated_programs}


def read_gradebook_rules(tenant, keys=RULE_KEYS) -> GradebookRules:
    """The settings named in *keys*, cleaned; every other one at its default.

    One query for the definitions and one per key, so a caller needing one
    setting asks for that one. With no tenant, the defaults.
    """
    if tenant is None:
        return GradebookRules()
    from schools.vs_academics.services.academic_rules import resolve_many

    found = resolve_many(keys, tenant=tenant)
    values = {}
    if CFG_SHOW_CLASS_STATS in keys:
        values["show_class_stats"] = _flag(
            found.get(CFG_SHOW_CLASS_STATS), DEFAULT_SHOW_CLASS_STATS,
        )
    if CFG_RANK_IN_CLASS in keys:
        values["rank_in_class"] = _flag(found.get(CFG_RANK_IN_CLASS), DEFAULT_RANK_IN_CLASS)
    if CFG_CONDUCT_RATINGS in keys:
        values["conduct_ratings"] = _flag(
            found.get(CFG_CONDUCT_RATINGS), DEFAULT_CONDUCT_RATINGS,
        )
    if CFG_RATING_KEY in keys:
        values["rating_key"] = _flag(found.get(CFG_RATING_KEY), DEFAULT_RATING_KEY)
    if CFG_RATING_TRAITS in keys:
        values["rating_traits"] = clean_traits(found.get(CFG_RATING_TRAITS)) or DEFAULT_RATING_TRAITS
    if CFG_MODERATED_PROGRAMS in keys:
        values["moderated_programs"] = clean_program_codes(found.get(CFG_MODERATED_PROGRAMS))
    if CFG_TELL_EVERY_PARENT in keys:
        values["tell_every_parent"] = _flag(
            found.get(CFG_TELL_EVERY_PARENT), DEFAULT_TELL_EVERY_PARENT,
        )
    return GradebookRules(**values)
