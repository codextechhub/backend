"""Reading and changing a school's attendance settings.

The four settings are tenant-wide ``vs_config`` values (:mod:`.rules`), so one
change binds every branch of the school. This module is the settings screen's
side of them: it builds the body the screen reads, which carries the school's
levels beside the values so the screen holds no copy of what a level is, and it
writes a change through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail.

**Levels are named by code.** Lesson marking is stored as the codes of the
levels it applies to, because a ``Level`` row belongs to one school year and a
switch on the row would reset every year. The screen offers the levels of the
active year, one entry per code however many programmes or branches carry it,
and a change is refused when it names a code that no level of any year, branch
or programme has ever had at this school. A code is saved in the spelling the
school's level uses, whatever case the request sent it in.

**A value already in force is not written again,** so saving the screen
unchanged leaves no audit rows. Every value is validated before the first is
written, and the writes share one transaction, so a refusal leaves nothing
half-saved.
"""
from __future__ import annotations

from django.db import transaction

from schools.vs_academics.models import AcademicSession, AcademicTerm, Level, SessionStatus
from schools.vs_calendar.models import ClassTimetable, PublishState
from vs_config.clock import tenant_today

from ..constants import (
    CFG_CHRONIC_THRESHOLD,
    CFG_EDIT_WINDOW_DAYS,
    CFG_GUARDIAN_NOTICES,
    CFG_LESSON_MARKING_LEVELS,
)
from ..exceptions import AttendanceSettingNotRegistered, UnknownLevelCodes
from .rules import AttendanceRules, read_attendance_rules

#: The request key of each setting and the definition it is stored in.
KEY_FOR_FIELD = {
    "guardian_notices": CFG_GUARDIAN_NOTICES,
    "chronic_threshold": CFG_CHRONIC_THRESHOLD,
    "lesson_marking_levels": CFG_LESSON_MARKING_LEVELS,
    "edit_window_days": CFG_EDIT_WINDOW_DAYS,
}

_WHY = "Attendance settings changed from the attendance settings screen."


def _active_session(tenant):
    return (
        AcademicSession.objects.filter(tenant=tenant, status=SessionStatus.ACTIVE)
        .order_by("-start_date", "-pk").first()
    )


def _published_codes(tenant) -> set:
    """The codes, lower-cased, of the levels with a class whose timetable is published this year."""
    return {
        code.lower()
        for code in ClassTimetable.objects.filter(
            tenant=tenant, session__status=SessionStatus.ACTIVE,
            status=PublishState.PUBLISHED, school_class__is_active=True,
        ).values_list("school_class__level__code", flat=True).distinct()
    }


def _levels_offered(tenant, marking) -> list:
    """The active year's levels, one entry per code, in programme and level order.

    ``has_published_timetable`` says whether any class at the level has a
    published timetable this year, which is what lesson marking needs before it
    has a lesson to mark.
    """
    published = _published_codes(tenant)
    rows = (
        Level.objects.filter(
            tenant=tenant, session__status=SessionStatus.ACTIVE, is_active=True,
        )
        .select_related("program")
        .order_by("program__order_index", "program__name", "order_index", "pk")
    )
    seen, out = set(), []
    for level in rows:
        key = level.code.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "code": level.code,
            "name": level.name,
            "program_id": level.program_id,
            "program_name": level.program.name,
            "lesson_marking": level.code in marking,
            "has_published_timetable": key in published,
        })
    return out


def _scope_label(tenant) -> dict:
    """The words a screen prints to say whose settings these are."""
    session = _active_session(tenant)
    term = None
    if session is not None:
        today = tenant_today(tenant)
        term = (
            AcademicTerm.objects.filter(
                tenant=tenant, session=session,
                start_date__lte=today, end_date__gte=today,
            ).first()
        )
    return {
        "school_name": tenant.name,
        "session_name": session.name if session is not None else None,
        "term_name": term.name if term is not None else None,
    }


def settings_body(tenant, rules: AttendanceRules | None = None) -> dict:
    """The body of ``GET /v1/attendance/settings/``."""
    rules = rules or read_attendance_rules(tenant)
    marking = set(rules.lesson_marking_levels)
    return {
        "guardian_notices": rules.guardian_notices,
        "chronic_threshold": rules.chronic_threshold,
        "lesson_marking_levels": list(rules.lesson_marking_levels),
        "edit_window_days": rules.edit_window_days,
        "levels": _levels_offered(tenant, marking),
        "scope_label": _scope_label(tenant),
    }


def resolve_level_codes(tenant, codes) -> list:
    """*codes* in the spelling the school's levels use, refusing any that no level has.

    Every level of the school counts, whatever its year, branch or programme,
    so a code switched on for a level that the active year has not created yet
    is still accepted.
    """
    known = {
        code.lower(): code
        for code in Level.objects.filter(tenant=tenant).values_list("code", flat=True)
    }
    resolved, unknown = [], []
    for code in codes:
        canonical = known.get(code.lower())
        if canonical is None:
            unknown.append(code)
        elif canonical not in resolved:
            resolved.append(canonical)
    if unknown:
        raise UnknownLevelCodes(
            "This school has no level with the code "
            + ("s " if len(unknown) > 1 else "")
            + ", ".join(unknown)
            + ". Choose from the levels listed.",
            field="lesson_marking_levels", codes=unknown,
        )
    return resolved


@transaction.atomic
def write_settings(tenant, actor, values: dict) -> AttendanceRules:
    """Store the settings named in *values*, leaving the others as they are.

    *values* is keyed by request field (:data:`KEY_FOR_FIELD`) and already
    validated for type and range; the level codes are checked against the
    school's levels here. Raises ``ATTENDANCE_SETTING_NOT_REGISTERED`` before
    anything is written when a definition is missing.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import set_value

    wanted = {KEY_FOR_FIELD[field]: value for field, value in values.items()}
    if CFG_LESSON_MARKING_LEVELS in wanted:
        wanted[CFG_LESSON_MARKING_LEVELS] = resolve_level_codes(
            tenant, wanted[CFG_LESSON_MARKING_LEVELS],
        )
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise AttendanceSettingNotRegistered(key=key)

    current = read_attendance_rules(tenant)
    in_force = {
        CFG_GUARDIAN_NOTICES: current.guardian_notices,
        CFG_CHRONIC_THRESHOLD: current.chronic_threshold,
        CFG_LESSON_MARKING_LEVELS: list(current.lesson_marking_levels),
        CFG_EDIT_WINDOW_DAYS: current.edit_window_days,
    }
    for key, value in wanted.items():
        if in_force[key] == value:
            continue
        set_value(
            definition=definitions[key], value=value, actor=actor, tenant=tenant,
            reason=_WHY,
        )
    return read_attendance_rules(tenant)
