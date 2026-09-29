"""A school's calendar and timetable settings.

Seven ``vs_config`` definitions, all school-scoped, read through
``resolve_value`` so a school's value, the platform's and the definition's
default are inherited exactly as every other setting is:

* ``calendar.teaching_days``: the weekdays the school teaches, ISO 1 (Monday)
  to 7 (Sunday), at least one, default Monday to Friday. They are the day
  columns of every class and teacher grid, the days a period or a lesson may be
  placed on, and the days the overview counts in "taught X of Y". One set for
  the whole school: a level never teaches different days from another.
* ``calendar.week_starts_on``: 1 (Monday) or 7 (Sunday), default 1. Date
  pickers and grids start their week on it, and the grids here order their day
  columns by it.
* ``calendar.closes_school_by_type``: for each of the six event types, whether
  an entry of that type closes the school when it is created without saying.
  Default: a public holiday and the half-term break close it; an exam period,
  a school event, a PTA meeting and a sports day do not. It fills in a missing
  ``closes_school`` on create and on import, and never changes a stored event.
* ``timetable.room_required_to_publish``: whether the publish gate refuses a
  lesson with no room, default true. A lesson with no teacher is refused either
  way.
* ``timetable.teacher_duty_match``: OFF, WARN or REFUSE, default OFF, for a
  lesson whose teacher holds no teaching duty (``vs_staff``) for its class and
  subject in its session. See ``services.duties``.
* ``exams.invigilator_roles``: the keys of the school's roles whose active
  holders may invigilate a paper, default ``["teacher"]``.
* ``timetable.default_period_minutes``: 10 to 240, or null for none (the
  default). Only exposed: the period form pre-fills an end time from it.

A value stored by hand at the platform layer can be anything its type allows,
so every read is cleaned here rather than trusted: a value that breaks a rule
the settings screen enforces reads as the default. A bad stored value then
costs a school its own choice, never a screen.

Writes go through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail. A value already in force
is not written again, so saving the screen unchanged leaves no audit rows. A
default period length set back to null clears the school's own value
(``config.value.cleared``), because a null is not a storable integer: the
school then reads the platform's value, which is null unless the platform sets
one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from ..constants import (
    CFG_CLOSES_SCHOOL_BY_TYPE,
    CFG_DEFAULT_PERIOD_MINUTES,
    CFG_INVIGILATOR_ROLES,
    CFG_ROOM_REQUIRED_TO_PUBLISH,
    CFG_TEACHER_DUTY_MATCH,
    CFG_TEACHING_DAYS,
    CFG_WEEK_STARTS_ON,
    DEFAULT_INVIGILATOR_ROLES,
    DEFAULT_TEACHING_DAYS,
    PERIOD_MINUTES_MAX,
    PERIOD_MINUTES_MIN,
    WEEK_STARTS,
    DutyMatch,
)
from ..exceptions import CalendarSettingNotRegistered
from ..models import EventType

RULE_KEYS = (
    CFG_TEACHING_DAYS,
    CFG_WEEK_STARTS_ON,
    CFG_CLOSES_SCHOOL_BY_TYPE,
    CFG_ROOM_REQUIRED_TO_PUBLISH,
    CFG_TEACHER_DUTY_MATCH,
    CFG_INVIGILATOR_ROLES,
    CFG_DEFAULT_PERIOD_MINUTES,
)

#: Whether each event type closes the school when an entry does not say.
DEFAULT_CLOSES_SCHOOL = {
    EventType.HOLIDAY.value: True,
    EventType.MIDTERM_BREAK.value: True,
    EventType.EXAM_PERIOD.value: False,
    EventType.SCHOOL_EVENT.value: False,
    EventType.PTA.value: False,
    EventType.SPORTS.value: False,
}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def teaching_days_problem(value) -> tuple[str, object] | None:
    """Why *value* is not a usable set of teaching days, as ``(cause, entry)``, or None.

    The cause is ``empty`` (not a list, or an empty one), ``not_a_day`` (an
    entry that is not an ISO weekday) or ``duplicate``. The serializer words
    the refusal; the reader uses the answer to decide whether a stored value
    can be trusted.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return "empty", None
    seen = set()
    for item in value:
        if not _is_int(item) or not 1 <= item <= 7:
            return "not_a_day", item
        if item in seen:
            return "duplicate", item
        seen.add(item)
    return None


def _clean_days(value) -> tuple:
    if value is None or teaching_days_problem(value) is not None:
        return DEFAULT_TEACHING_DAYS
    return tuple(sorted(value))


def _clean_week_start(value) -> int:
    return value if _is_int(value) and value in WEEK_STARTS else WEEK_STARTS[0]


def _clean_closes(value) -> dict:
    stored = value if isinstance(value, dict) else {}
    return {
        kind: stored[kind] if isinstance(stored.get(kind), bool) else default
        for kind, default in DEFAULT_CLOSES_SCHOOL.items()
    }


def _clean_roles(value) -> tuple:
    if not isinstance(value, (list, tuple)) or not value:
        return DEFAULT_INVIGILATOR_ROLES
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return DEFAULT_INVIGILATOR_ROLES
        if item.strip() not in out:
            out.append(item.strip())
    return tuple(out)


def _clean_minutes(value):
    if _is_int(value) and PERIOD_MINUTES_MIN <= value <= PERIOD_MINUTES_MAX:
        return value
    return None


@dataclass(frozen=True)
class CalendarRules:
    """The seven settings, cleaned, with every default a school starts on."""

    teaching_days: tuple = DEFAULT_TEACHING_DAYS
    week_starts_on: int = WEEK_STARTS[0]
    closes_school_by_type: dict = field(
        default_factory=lambda: dict(DEFAULT_CLOSES_SCHOOL),
    )
    room_required_to_publish: bool = True
    teacher_duty_match: str = DutyMatch.OFF.value
    invigilator_roles: tuple = DEFAULT_INVIGILATOR_ROLES
    default_period_minutes: int | None = None

    def grid_days(self, held=()) -> list:
        """The day columns of a grid, in the order the school's week is drawn.

        The teaching days, plus any day in *held*: the days a grid actually
        holds lessons on. A lesson on a day the school has stopped teaching is
        drawn rather than hidden, so it can be seen and cleared: it still
        counts in clashes, and it blocks publishing until it is moved or
        removed. The order
        starts from ``week_starts_on``, so a Sunday-start school teaching
        Sunday to Thursday draws Sunday first.
        """
        start = self.week_starts_on
        days = set(self.teaching_days) | {int(day) for day in held}
        return sorted(days, key=lambda day: (day - start) % 7)


def read_calendar_rules(tenant, keys=RULE_KEYS) -> CalendarRules:
    """The settings named in *keys*, cleaned; every other one at its default.

    One query for the definitions and one per key, so a caller needing one
    setting asks for that one. With no tenant, the defaults.
    """
    if tenant is None:
        return CalendarRules()
    from schools.vs_academics.services.academic_rules import resolve_many

    found = resolve_many(keys, tenant=tenant)
    values = {}
    if CFG_TEACHING_DAYS in keys:
        values["teaching_days"] = _clean_days(found.get(CFG_TEACHING_DAYS))
    if CFG_WEEK_STARTS_ON in keys:
        values["week_starts_on"] = _clean_week_start(found.get(CFG_WEEK_STARTS_ON))
    if CFG_CLOSES_SCHOOL_BY_TYPE in keys:
        values["closes_school_by_type"] = _clean_closes(
            found.get(CFG_CLOSES_SCHOOL_BY_TYPE),
        )
    if CFG_ROOM_REQUIRED_TO_PUBLISH in keys:
        stored = found.get(CFG_ROOM_REQUIRED_TO_PUBLISH)
        values["room_required_to_publish"] = (
            stored if isinstance(stored, bool) else True
        )
    if CFG_TEACHER_DUTY_MATCH in keys:
        stored = found.get(CFG_TEACHER_DUTY_MATCH)
        values["teacher_duty_match"] = (
            stored if stored in DutyMatch.values else DutyMatch.OFF.value
        )
    if CFG_INVIGILATOR_ROLES in keys:
        values["invigilator_roles"] = _clean_roles(found.get(CFG_INVIGILATOR_ROLES))
    if CFG_DEFAULT_PERIOD_MINUTES in keys:
        values["default_period_minutes"] = _clean_minutes(
            found.get(CFG_DEFAULT_PERIOD_MINUTES),
        )
    return CalendarRules(**values)


def read_teaching_days(tenant) -> tuple:
    """The school's teaching days, ascending ISO weekdays."""
    return read_calendar_rules(tenant, (CFG_TEACHING_DAYS,)).teaching_days


def read_grid_days(tenant) -> list:
    """The teaching days in the order the school's week is drawn."""
    return read_calendar_rules(
        tenant, (CFG_TEACHING_DAYS, CFG_WEEK_STARTS_ON),
    ).grid_days()


def closes_school_default(tenant, event_type) -> bool:
    """Whether an entry of *event_type* closes the school when it does not say."""
    closes = read_calendar_rules(
        tenant, (CFG_CLOSES_SCHOOL_BY_TYPE,),
    ).closes_school_by_type
    return closes.get(event_type, False)


def read_duty_match(tenant) -> str:
    return read_calendar_rules(tenant, (CFG_TEACHER_DUTY_MATCH,)).teacher_duty_match


def read_room_required(tenant) -> bool:
    return read_calendar_rules(
        tenant, (CFG_ROOM_REQUIRED_TO_PUBLISH,),
    ).room_required_to_publish


def read_invigilator_roles(tenant) -> tuple:
    return read_calendar_rules(tenant, (CFG_INVIGILATOR_ROLES,)).invigilator_roles


def active_roles(tenant) -> list:
    """The school's active roles as ``(key, name)``, by name."""
    from vs_rbac.models import TenantRoleTemplate

    return list(
        TenantRoleTemplate.objects.filter(tenant=tenant, status="ACTIVE")
        .order_by("name", "key")
        .values_list("key", "name"),
    )


def calendar_rules_body(tenant, rules=None) -> dict:
    """The body of ``GET /v1/academics/calendar/rules/``.

    Each choice the screen offers travels with the label it prints, so the
    screen carries no copy of them: the six event types in the school's word
    for a term ("Mid-semester break"), the three duty-match modes, and the
    school's active roles. A saved role that is no longer active is left out of
    ``invigilator_roles``, because nobody holds it and the screen could not
    show it as a choice.
    """
    from .calendar import event_type_label
    from schools.vs_academics.services.academic_rules import read_term_word

    rules = rules or read_calendar_rules(tenant)
    word = read_term_word(tenant)
    roles = active_roles(tenant) if tenant is not None else []
    active = {key for key, _ in roles}
    return {
        "teaching_days": list(rules.teaching_days),
        "week_starts_on": rules.week_starts_on,
        "closes_school_by_type": dict(rules.closes_school_by_type),
        "event_types": [
            {"value": value, "label": event_type_label(value, word)}
            for value, _ in EventType.choices
        ],
        "room_required_to_publish": rules.room_required_to_publish,
        "teacher_duty_match": rules.teacher_duty_match,
        "teacher_duty_match_options": [
            {"value": value, "label": label} for value, label in DutyMatch.choices
        ],
        "invigilator_roles": [
            key for key in rules.invigilator_roles if key in active
        ],
        "invigilator_role_options": [
            {"value": key, "label": name} for key, name in roles
        ],
        "default_period_minutes": rules.default_period_minutes,
    }


@transaction.atomic
def write_calendar_rules(
    tenant, actor, *, teaching_days, week_starts_on, closes_school_by_type,
    room_required_to_publish, teacher_duty_match, invigilator_roles,
    default_period_minutes, reason="",
) -> CalendarRules:
    """Store the school's calendar and timetable settings. The caller has already validated them.

    Refused with ``CALENDAR_SETTING_NOT_REGISTERED`` before anything is
    written when a definition is missing, so a half-saved set never exists.
    Each value is compared with what the school reads now, defaults included,
    so saving what the screen showed writes nothing.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import clear_value, set_value

    wanted = {
        CFG_TEACHING_DAYS: sorted(teaching_days),
        CFG_WEEK_STARTS_ON: week_starts_on,
        CFG_CLOSES_SCHOOL_BY_TYPE: {
            kind: bool(closes_school_by_type[kind]) for kind in DEFAULT_CLOSES_SCHOOL
        },
        CFG_ROOM_REQUIRED_TO_PUBLISH: room_required_to_publish,
        CFG_TEACHER_DUTY_MATCH: teacher_duty_match,
        CFG_INVIGILATOR_ROLES: list(invigilator_roles),
        CFG_DEFAULT_PERIOD_MINUTES: default_period_minutes,
    }
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise CalendarSettingNotRegistered(key=key)

    current = read_calendar_rules(tenant)
    in_force = {
        CFG_TEACHING_DAYS: list(current.teaching_days),
        CFG_WEEK_STARTS_ON: current.week_starts_on,
        CFG_CLOSES_SCHOOL_BY_TYPE: dict(current.closes_school_by_type),
        CFG_ROOM_REQUIRED_TO_PUBLISH: current.room_required_to_publish,
        CFG_TEACHER_DUTY_MATCH: current.teacher_duty_match,
        CFG_INVIGILATOR_ROLES: list(current.invigilator_roles),
        CFG_DEFAULT_PERIOD_MINUTES: current.default_period_minutes,
    }
    why = (reason or "").strip() or "Calendar and timetable set from School settings."
    for key, value in wanted.items():
        if in_force[key] == value:
            continue
        if value is None:
            clear_value(
                definition=definitions[key], actor=actor, tenant=tenant, reason=why,
            )
            continue
        set_value(
            definition=definitions[key], value=value, actor=actor, tenant=tenant,
            reason=why,
        )
    return read_calendar_rules(tenant)
