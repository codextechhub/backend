"""Reading and changing a school's gradebook settings.

The seven settings are tenant-wide ``vs_config`` values (:mod:`.rules`), so one
change binds every branch of the school. This module is the settings screen's
side of them: it builds the body the screen reads, which carries the school's
programmes beside the values so the screen holds no copy of what a programme is,
and it writes a change through ``set_value``, which checks the definition's
scope and records ``config.value.updated`` in the audit trail.

**Programmes are named by code.** Moderation is stored as the codes of the
programmes it applies to, as attendance stores level codes, so a programme
renamed or recreated for a new year keeps the school's choice. A change is
refused when it names a code no programme of this school has, and a code is
saved in the spelling the school's programme uses, whatever case the request
sent it in.

**A value already in force is not written again,** so saving the screen
unchanged leaves no audit rows. Every value is validated before the first is
written, and the writes share one transaction, so a refusal leaves nothing
half-saved.
"""
from __future__ import annotations

from django.db import transaction

from schools.vs_academics.models import Program

from ..constants import (
    CFG_CONDUCT_RATINGS,
    CFG_MODERATED_PROGRAMS,
    CFG_RANK_IN_CLASS,
    CFG_RATING_KEY,
    CFG_RATING_TRAITS,
    CFG_SHOW_CLASS_STATS,
    CFG_TELL_EVERY_PARENT,
)
from ..exceptions import GradebookSettingNotRegistered, UnknownProgramCodes
from .rules import GradebookRules, read_gradebook_rules

#: The request key of each setting and the definition it is stored in. The
#: settings search places are anchored at these names.
KEY_FOR_FIELD = {
    "show_class_stats": CFG_SHOW_CLASS_STATS,
    "rank_in_class": CFG_RANK_IN_CLASS,
    "conduct_ratings": CFG_CONDUCT_RATINGS,
    "rating_key": CFG_RATING_KEY,
    "rating_traits": CFG_RATING_TRAITS,
    "moderated_programs": CFG_MODERATED_PROGRAMS,
    "tell_every_parent": CFG_TELL_EVERY_PARENT,
}

_WHY = "Gradebook settings changed from the assessment setup screen."


def programs_offered(tenant, moderated) -> list:
    """The school's programmes, one entry per code, in programme order.

    ``moderated`` says whether sheets of the programme go to the head of the
    subject's department first.
    """
    marked = {code.lower() for code in moderated}
    seen, out = set(), []
    rows = Program.objects.filter(tenant=tenant, is_active=True).order_by(
        "order_index", "name", "pk",
    )
    for program in rows:
        key = program.code.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "code": program.code,
            "name": program.name,
            "moderated": key in marked,
        })
    return out


def settings_body(tenant, rules: GradebookRules | None = None) -> dict:
    """The body of ``GET /v1/gradebook/settings/``."""
    rules = rules or read_gradebook_rules(tenant)
    return {
        "show_class_stats": rules.show_class_stats,
        "rank_in_class": rules.rank_in_class,
        "conduct_ratings": rules.conduct_ratings,
        "rating_key": rules.rating_key,
        "rating_traits": list(rules.rating_traits),
        "moderated_programs": list(rules.moderated_programs),
        "programs": programs_offered(tenant, rules.moderated_programs),
        "tell_every_parent": rules.tell_every_parent,
    }


def resolve_program_codes(tenant, codes) -> list:
    """*codes* in the spelling the school's programmes use, refusing any that no programme has."""
    known = {
        code.lower(): code
        for code in Program.objects.filter(tenant=tenant).values_list("code", flat=True)
    }
    resolved, unknown = [], []
    for code in codes:
        canonical = known.get(code.lower())
        if canonical is None:
            unknown.append(code)
        elif canonical not in resolved:
            resolved.append(canonical)
    if unknown:
        raise UnknownProgramCodes(
            "This school has no programme with the code"
            + ("s " if len(unknown) > 1 else " ")
            + ", ".join(unknown)
            + ". Choose from the programmes listed.",
            field="moderated_programs", codes=unknown,
        )
    return resolved


@transaction.atomic
def write_settings(tenant, actor, values: dict) -> GradebookRules:
    """Store the settings named in *values*, leaving the others as they are.

    *values* is keyed by request field (:data:`KEY_FOR_FIELD`) and already
    validated for type and range; the programme codes are checked against the
    school's programmes here. Raises ``GRADEBOOK_SETTING_NOT_REGISTERED`` before
    anything is written when a definition is missing.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import set_value

    wanted = {KEY_FOR_FIELD[field]: value for field, value in values.items()}
    if CFG_MODERATED_PROGRAMS in wanted:
        wanted[CFG_MODERATED_PROGRAMS] = resolve_program_codes(
            tenant, wanted[CFG_MODERATED_PROGRAMS],
        )
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise GradebookSettingNotRegistered(key=key)

    current = read_gradebook_rules(tenant)
    in_force = {
        CFG_SHOW_CLASS_STATS: current.show_class_stats,
        CFG_RANK_IN_CLASS: current.rank_in_class,
        CFG_CONDUCT_RATINGS: current.conduct_ratings,
        CFG_RATING_KEY: current.rating_key,
        CFG_RATING_TRAITS: list(current.rating_traits),
        CFG_MODERATED_PROGRAMS: list(current.moderated_programs),
        CFG_TELL_EVERY_PARENT: current.tell_every_parent,
    }
    for key, value in wanted.items():
        if in_force[key] == value:
            continue
        set_value(
            definition=definitions[key], value=value, actor=actor, tenant=tenant,
            reason=_WHY,
        )
    return read_gradebook_rules(tenant)
