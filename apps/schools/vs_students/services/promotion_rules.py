"""A school's own promotion rules: suspended pupils, unplaced pupils, arms, capacity.

Four ``vs_config`` definitions decide what the promotion does, all school-scoped, read through
``rules.resolve_many`` so inheritance is vs_config's own. Each default is the
behaviour every school had before it could choose, so a school that has set
nothing is promoted exactly as before:

* a suspended pupil is listed as an exception and not moved (``HOLD``);
* a pupil who is confirmed but not placed is held where they are (``HOLD``);
* each arm moves up whole, JSS1 B to JSS2 B (``SAME_ARM``);
* a class the run would fill past its capacity is treated as the enrolment
  rule treats one (``FOLLOW_ENROLMENT``).

A fifth, the **pass mark**, decides nothing. It is the average the school
treats as the mark for moving up, shown to staff on the promotion screen and
in the preview so the person recording outcomes has it in front of them.
Promotion stays the outcome a member of staff records; a pupil averaging 45%
against a 50% mark is promoted if the school decides so, and the portal never
shows a pupil the figure. Empty means the school has set none.

``services/promotion.py`` reads them once per classification, so the preview
and the run apply the same rules. Its ``Plan.capacity_mode`` is the EFFECTIVE
capacity mode: the enrolment rule where the school follows it, the school's
own promotion rule otherwise.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: an unknown value reads as
the default. A bad stored value then costs a school its own rule, never a
promotion.

Writes go through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail. A value already in force
is not written again, so saving the screen unchanged leaves no audit rows.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ..constants import (
    CFG_CAPACITY_MODE,
    CFG_PROMOTION_ARMS,
    CFG_PROMOTION_CAPACITY_MODE,
    CFG_PROMOTION_NOT_PLACED,
    CFG_PROMOTION_PASS_MARK,
    CFG_PROMOTION_SUSPENDED,
    CapacityMode,
    PromotionArms,
    PromotionCapacityMode,
    PromotionNotPlaced,
    PromotionSuspended,
)
from ..exceptions import StudentSettingNotRegistered
from .rules import resolve_many

#: The school's own four, in the order the screen shows them.
_RULE_KEYS = (
    CFG_PROMOTION_SUSPENDED, CFG_PROMOTION_NOT_PLACED, CFG_PROMOTION_ARMS,
    CFG_PROMOTION_CAPACITY_MODE,
)

#: What a write leaves alone: the pass mark was not sent.
UNCHANGED = object()

PASS_MARK_MIN, PASS_MARK_MAX = 1, 100


def _pass_mark(value):
    """A stored pass mark, or None when unset or not a mark at all."""
    if isinstance(value, bool):
        return None
    try:
        mark = int(value)
    except (TypeError, ValueError):
        return None
    return mark if PASS_MARK_MIN <= mark <= PASS_MARK_MAX else None


def _choice(value, choices, default) -> str:
    return str(value) if value in choices.values else default.value


def _options(choices) -> list[dict]:
    return [{"value": value, "label": label} for value, label in choices.choices]


@dataclass(frozen=True)
class PromotionRules:
    suspended: str = PromotionSuspended.HOLD.value
    not_placed: str = PromotionNotPlaced.HOLD.value
    arms: str = PromotionArms.SAME_ARM.value
    capacity_mode: str = PromotionCapacityMode.FOLLOW_ENROLMENT.value
    #: The enrolment capacity rule, which FOLLOW_ENROLMENT applies.
    enrolment_capacity_mode: str = CapacityMode.WARN.value
    #: For guidance only; None when the school sets none.
    pass_mark: int | None = None

    @property
    def effective_capacity_mode(self) -> str:
        """WARN, HARD or OFF: what the run does with a class it would overfill."""
        if self.capacity_mode == PromotionCapacityMode.FOLLOW_ENROLMENT:
            return self.enrolment_capacity_mode
        return self.capacity_mode

    def stored(self) -> dict:
        """The rules as the school saved them, for the promotion preview."""
        return {
            "suspended": self.suspended,
            "not_placed": self.not_placed,
            "arms": self.arms,
            "capacity_mode": self.capacity_mode,
            "pass_mark": self.pass_mark,
        }

    def as_dict(self) -> dict:
        """The body of ``GET /v1/students/promotion-rules/``.

        ``options`` are the choices the screen offers, each with the label it
        prints, so the screen carries no copy of them.
        """
        return {
            **self.stored(),
            "effective_capacity_mode": self.effective_capacity_mode,
            "enrolment_capacity_mode": self.enrolment_capacity_mode,
            "options": {
                "suspended": _options(PromotionSuspended),
                "not_placed": _options(PromotionNotPlaced),
                "arms": _options(PromotionArms),
                "capacity_mode": _options(PromotionCapacityMode),
            },
        }


def read_promotion_rules(tenant) -> PromotionRules:
    """Every rule, the pass mark and the enrolment capacity rule in one read.

    With no tenant, the defaults, which is how every school promoted before it
    could choose.
    """
    if tenant is None:
        return PromotionRules()
    found = {
        key: value
        for key, (value, _) in resolve_many(
            (*_RULE_KEYS, CFG_PROMOTION_PASS_MARK, CFG_CAPACITY_MODE), tenant=tenant,
        ).items()
    }
    return PromotionRules(
        suspended=_choice(
            found.get(CFG_PROMOTION_SUSPENDED), PromotionSuspended,
            PromotionSuspended.HOLD,
        ),
        not_placed=_choice(
            found.get(CFG_PROMOTION_NOT_PLACED), PromotionNotPlaced,
            PromotionNotPlaced.HOLD,
        ),
        arms=_choice(
            found.get(CFG_PROMOTION_ARMS), PromotionArms, PromotionArms.SAME_ARM,
        ),
        capacity_mode=_choice(
            found.get(CFG_PROMOTION_CAPACITY_MODE), PromotionCapacityMode,
            PromotionCapacityMode.FOLLOW_ENROLMENT,
        ),
        enrolment_capacity_mode=_choice(
            found.get(CFG_CAPACITY_MODE), CapacityMode, CapacityMode.WARN,
        ),
        pass_mark=_pass_mark(found.get(CFG_PROMOTION_PASS_MARK)),
    )


@transaction.atomic
def write_promotion_rules(
    tenant, actor, *, suspended, not_placed, arms, capacity_mode,
    pass_mark=UNCHANGED, reason="",
) -> PromotionRules:
    """Store the school's promotion rules. The caller has already validated them.

    ``pass_mark`` is left as it is when not given, set when given a mark, and
    cleared when given None.

    Refused with ``STUDENT_SETTING_NOT_REGISTERED`` before anything is written
    when a definition is missing, so a half-saved set never exists.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value, set_value

    wanted = {
        CFG_PROMOTION_SUSPENDED: suspended,
        CFG_PROMOTION_NOT_PLACED: not_placed,
        CFG_PROMOTION_ARMS: arms,
        CFG_PROMOTION_CAPACITY_MODE: capacity_mode,
    }
    keys = [*wanted, *([] if pass_mark is UNCHANGED else [CFG_PROMOTION_PASS_MARK])]
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=keys, is_active=True,
        )
    }
    for key in keys:
        if key not in definitions:
            raise StudentSettingNotRegistered(key=key)

    why = (reason or "").strip() or "Promotion rules set from Student settings."
    for key, value in wanted.items():
        definition = definitions[key]
        current, _ = resolve_value(definition, tenant=tenant)
        if current == value:
            continue
        set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason=why,
        )
    if pass_mark is not UNCHANGED:
        _write_pass_mark(definitions[CFG_PROMOTION_PASS_MARK], tenant, actor, pass_mark, why)
    return read_promotion_rules(tenant)


def _write_pass_mark(definition, tenant, actor, mark, why):
    """Set the pass mark, or clear it for None; a mark already in force is left."""
    from vs_config.services.resolution import clear_value, resolve_value, set_value

    current, _ = resolve_value(definition, tenant=tenant)
    if _pass_mark(current) == mark:
        return
    if mark is None:
        clear_value(definition=definition, actor=actor, tenant=tenant, reason=why)
    else:
        set_value(definition=definition, value=mark, actor=actor, tenant=tenant, reason=why)
