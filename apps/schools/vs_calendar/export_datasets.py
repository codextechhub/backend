"""This module's datasets, published to the Export Centre.

Registered from :meth:`schools.vs_calendar.apps.VsCalendarConfig.ready`.
Registration lives here rather than in ``vs_exports`` so the engine never
imports a domain app, which is the pattern ``schools.vs_schools`` established
and ``schools.vs_academics`` follows.

**Branch narrowing is applied, and it is the same rule the screens use.** Every
dataset narrows through :func:`vs_exports.catalogue.narrow_to_caller_branches`,
which defers to ``vs_rbac.scoping.visible_branch_ids`` - the platform's one
authority on which branches a caller may see. So an export and the list it
mirrors cannot answer differently.

The reading is **inclusive** for events and periods: a row with no branch is
shared by the whole school and everybody sees it, which is right for a calendar
where the shared rows are most of it. Rooms carry a non-null branch, so the
question does not arise for them.

**No person is exported as an email address**, and this matters more here than
anywhere else in the platform: a class timetable is the most widely read
document a school produces, and an export of one is the version that gets
emailed around. The teacher and invigilator columns are display names.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_BOOLEAN,
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    KIND_CHOICE,
    KIND_DATE,
    KIND_DATETIME,
    KIND_NUMBER,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    choice_labels,
    narrow_to_caller_branches,
    register,
)

_MODULE = "Timetable & Calendar"

_EVENT_TYPES = choice_labels("schools.vs_calendar.models.EventType")
_ROOM_TYPES = choice_labels("schools.vs_calendar.models.RoomType")
_PERIOD_TYPES = choice_labels("schools.vs_calendar.models.PeriodType")
_SITTINGS = choice_labels("schools.vs_calendar.models.Sitting")

_SCOPE_FIELD = Field("branch__name", "Branch", "Scope", KIND_TEXT)
_ACTIVE_FILTER = FilterDef("is_active", "Active", FILTER_BOOLEAN)


def _events(scope):
    from .models import CalendarEvent

    return narrow_to_caller_branches(
        CalendarEvent.all_objects.filter(tenant=scope.tenant), scope,
    )


def _rooms(scope):
    from .models import Room

    return narrow_to_caller_branches(
        Room.all_objects.filter(tenant=scope.tenant), scope,
    )


def _periods(scope):
    from .models import Period

    return narrow_to_caller_branches(
        Period.all_objects.filter(tenant=scope.tenant), scope,
    )


def _slots(scope):
    """Lessons, narrowed by the ROOM's branch rather than by a column.

    A slot carries no branch of its own - the room answers that question, and a
    second column would be free to contradict it.

    ``inclusive`` is what keeps an unfilled slot in the file: a lesson with no
    room yet is a grid still being built, and the inclusive reading of a NULL
    branch keeps it visible to everyone rather than filtering it into nobody's
    export.
    """
    from .models import TimetableSlot

    return narrow_to_caller_branches(
        TimetableSlot.all_objects.filter(tenant=scope.tenant), scope,
        field="room__branch", inclusive=True,
    )


def _exam_slots(scope):
    from .models import ExamSlot

    return narrow_to_caller_branches(
        ExamSlot.all_objects.filter(tenant=scope.tenant), scope,
        field="room__branch", inclusive=True,
    )


def register_datasets():
    """Called once from AppConfig.ready()."""

    register(Dataset(
        key="calendar.events",
        module=_MODULE,
        name="Calendar events",
        description=(
            "Holidays, breaks, exam periods and school events, with the term "
            "each falls in and whether it closes the school."
        ),
        base=_events,
        scope=DatasetScope.TENANT,
        permission="academics.calendar.view",
        row_cap=10_000,
        default_columns=("name", "event_type", "start_date", "end_date", "branch__name"),
        fields=(
            Field("name", "Event", "Event", KIND_TEXT, locked=True),
            Field("event_type", "Type", "Event", KIND_CHOICE, choices=_EVENT_TYPES),
            Field("start_date", "Starts", "Event", KIND_DATE),
            Field("end_date", "Ends", "Event", KIND_DATE),
            Field("closes_school", "School closed", "Event", KIND_TEXT),
            Field("description", "Description", "Event", KIND_TEXT),
            _SCOPE_FIELD,
            Field("session__name", "Session", "Year", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("start_date", "Starts", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("event_type", "Type", FILTER_CHOICE, choices=_EVENT_TYPES),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("description", "Description"),
            )),
        ),
    ))

    register(Dataset(
        key="calendar.rooms",
        module=_MODULE,
        name="Rooms",
        description=(
            "The places lessons and examinations happen in, with the branch "
            "each belongs to. Capacity is advisory and nothing compares it "
            "with anything."
        ),
        base=_rooms,
        scope=DatasetScope.TENANT,
        permission="academics.timetable.view",
        row_cap=10_000,
        default_columns=("name", "code", "room_type", "branch__name", "capacity"),
        fields=(
            Field("name", "Room", "Room", KIND_TEXT, locked=True),
            Field("code", "Code", "Room", KIND_TEXT),
            Field("room_type", "Type", "Room", KIND_CHOICE, choices=_ROOM_TYPES),
            Field("capacity", "Capacity", "Room", KIND_NUMBER),
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("room_type", "Type", FILTER_CHOICE, choices=_ROOM_TYPES),
            _ACTIVE_FILTER,
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))

    register(Dataset(
        key="calendar.periods",
        module=_MODULE,
        name="Bell schedule",
        description=(
            "The daily period structure every timetable grid is built on. A "
            "row with no day applies to every teaching day; a row with a day "
            "replaces the everyday schedule on that day."
        ),
        base=_periods,
        scope=DatasetScope.TENANT,
        permission="academics.timetable.view",
        row_cap=10_000,
        default_columns=("label", "start_time", "end_time", "period_type", "branch__name"),
        fields=(
            Field("label", "Period", "Period", KIND_TEXT, locked=True),
            Field("order_index", "Order", "Period", KIND_NUMBER),
            Field("start_time", "Starts", "Period", KIND_TEXT),
            Field("end_time", "Ends", "Period", KIND_TEXT),
            Field("period_type", "Type", "Period", KIND_CHOICE, choices=_PERIOD_TYPES),
            Field("day_of_week", "Applies on", "Period", KIND_NUMBER),
            _SCOPE_FIELD,
            Field("session__name", "Session", "Year", KIND_TEXT),
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("period_type", "Type", FILTER_CHOICE, choices=_PERIOD_TYPES),
            _ACTIVE_FILTER,
            FilterDef("search", "Search", FILTER_SEARCH, searches=(("label", "Label"),)),
        ),
    ))

    register(Dataset(
        key="calendar.timetable",
        module=_MODULE,
        name="Class timetables",
        description=(
            "Every lesson in the school's week: the class, the day, the "
            "period, the subject, the teacher and the room."
        ),
        base=_slots,
        scope=DatasetScope.TENANT,
        permission="academics.timetable.view",
        row_cap=50_000,
        default_columns=(
            "school_class__name", "day_of_week", "period__label",
            "subject__name", "teacher__first_name", "room__name",
        ),
        fields=(
            Field("school_class__name", "Class", "Lesson", KIND_TEXT, locked=True),
            Field("day_of_week", "Day", "Lesson", KIND_NUMBER),
            Field("period__label", "Period", "Lesson", KIND_TEXT),
            Field("period__start_time", "Starts", "Lesson", KIND_TEXT),
            Field("subject__name", "Subject", "Lesson", KIND_TEXT),
            # Display names, never the address: an exported timetable is the
            # version that gets emailed around.
            Field("teacher__first_name", "Teacher first name", "People", KIND_TEXT),
            Field("teacher__last_name", "Teacher last name", "People", KIND_TEXT),
            Field("room__name", "Room", "Place", KIND_TEXT),
            Field("room__branch__name", "Branch", "Place", KIND_TEXT),
            Field("session__name", "Session", "Year", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("school_class__name", "Class"), ("subject__name", "Subject"),
            )),
        ),
    ))

    register(Dataset(
        key="calendar.exam_papers",
        module=_MODULE,
        name="Exam timetables",
        description=(
            "Papers placed inside an exam period: the class, the subject, the "
            "date, the sitting, the room and the invigilator."
        ),
        base=_exam_slots,
        scope=DatasetScope.TENANT,
        permission="academics.timetable.view",
        row_cap=50_000,
        default_columns=(
            "exam_date", "sitting", "school_class__name", "subject__name",
            "room__name",
        ),
        fields=(
            Field("exam_date", "Date", "Paper", KIND_DATE, locked=True),
            Field("sitting", "Sitting", "Paper", KIND_CHOICE, choices=_SITTINGS),
            Field("school_class__name", "Class", "Paper", KIND_TEXT),
            Field("subject__name", "Subject", "Paper", KIND_TEXT),
            Field("start_time", "Starts", "Paper", KIND_TEXT),
            Field("end_time", "Ends", "Paper", KIND_TEXT),
            Field("room__name", "Room", "Place", KIND_TEXT),
            Field("room__branch__name", "Branch", "Place", KIND_TEXT),
            Field("invigilator__first_name", "Invigilator first name", "People", KIND_TEXT),
            Field("invigilator__last_name", "Invigilator last name", "People", KIND_TEXT),
            Field("exam__name", "Exam", "Exam", KIND_TEXT),
            Field("exam__status", "Status", "Exam", KIND_TEXT),
        ),
        filters=(
            FilterDef("exam_date", "Date", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("sitting", "Sitting", FILTER_CHOICE, choices=_SITTINGS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("school_class__name", "Class"), ("subject__name", "Subject"),
            )),
        ),
    ))


#: Params these screens carry that are not filters. Listed so they are not
#: reported as dropped: a page number is not a narrowing.
_IGNORE = ("page", "page_size", "tenant", "view", "session", "day", "on")

_BRANCH_REASON = (
    "The export already covers the branches you may see, so this filter is "
    "not applied on top of it."
)


def _common(params):
    """Search, and the branch note every screen here shares."""
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    search = str(params.get("search", "")).strip()
    if search:
        filters.append({"id": "search", "value": search})
    branch = str(params.get("branch", "")).strip()
    if branch:
        unmapped.append(Unmapped("branch", branch, _BRANCH_REASON))
    return filters, unmapped


def _choice(params, key, filter_id, allowed, filters, unmapped, noun):
    from vs_exports.catalogue import Unmapped

    raw = str(params.get(key, "")).strip()
    if not raw or raw.upper() in ("ALL", "ANY"):
        return
    if raw.upper() in allowed:
        filters.append({"id": filter_id, "value": [raw.upper()]})
    else:
        unmapped.append(Unmapped(
            key, raw,
            f"“{raw}” is not a {noun} the export knows, so the file is not "
            f"limited by it.",
        ))


def _translate_events(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = _common(params)
    _choice(params, "type", "event_type", _EVENT_TYPES, filters, unmapped,
            "event type")

    scope = str(params.get("scope", "")).strip()
    if scope and scope.lower() not in ("all", "any"):
        unmapped.append(Unmapped("scope", scope, _BRANCH_REASON))

    # A term is a date range rather than a column, and the range lives on the
    # session, which the export does not resolve. Reported rather than dropped:
    # the dangerous failure here is a file that silently covers the whole year.
    term = str(params.get("term", "")).strip()
    if term and term.lower() not in ("all", "any"):
        unmapped.append(Unmapped(
            "term", term,
            "A term is a range of dates rather than a column, so the export "
            "cannot narrow to one. Use the date filter for the term's dates, "
            "or the file will cover the whole year.",
        ))
    return filters, unmapped


def _translate_rooms(params):
    filters, unmapped = _common(params)
    _choice(params, "type", "room_type", _ROOM_TYPES, filters, unmapped,
            "room type")
    active = str(params.get("active", "")).strip().lower()
    if active in ("true", "1", "active"):
        filters.append({"id": "is_active", "value": True})
    elif active in ("false", "0", "inactive"):
        filters.append({"id": "is_active", "value": False})
    elif active and active not in ("all", "any"):
        from vs_exports.catalogue import Unmapped

        unmapped.append(Unmapped(
            "active", active,
            f"\u201c{active}\u201d is not a room status the export knows, so "
            f"the file covers active and inactive rooms alike.",
        ))
    return filters, unmapped


def _translate_periods(params):
    filters, unmapped = _common(params)
    _choice(params, "period_type", "period_type", _PERIOD_TYPES, filters,
            unmapped, "period type")
    return filters, unmapped


def _translate_timetable(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = _common(params)
    # The screen shows one class at a time; the dataset has no class filter,
    # because the useful export of a timetable is the whole school's week.
    # Said out loud rather than silently widened.
    school_class = str(params.get("school_class", "")).strip()
    if school_class:
        unmapped.append(Unmapped(
            "school_class", school_class,
            "The timetable export covers every class you can see rather than "
            "the one on screen. Filter the file by the Class column.",
        ))
    return filters, unmapped


def _translate_exams(params):
    filters, unmapped = _common(params)
    _choice(params, "sitting", "sitting", _SITTINGS, filters, unmapped, "sitting")
    return filters, unmapped


def register_screens():
    """Called once from AppConfig.ready(), after register_datasets()."""
    from vs_exports.catalogue import ScreenBinding, register_screen

    for key, label, dataset, translate, handles in (
        ("calendar.events", "Calendar - Events", "calendar.events",
         _translate_events, ("search", "type", "branch", "scope", "term")),
        ("calendar.rooms", "Timetable - Rooms", "calendar.rooms",
         _translate_rooms, ("search", "type", "branch", "active")),
        ("calendar.periods", "Timetable - Bell schedule", "calendar.periods",
         _translate_periods, ("search", "period_type", "branch")),
        ("calendar.timetable", "Timetable - Class timetables", "calendar.timetable",
         _translate_timetable, ("search", "school_class", "branch")),
        ("calendar.exam_papers", "Timetable - Exam timetables", "calendar.exam_papers",
         _translate_exams, ("search", "sitting", "branch")),
    ):
        register_screen(ScreenBinding(
            key=key, label=label, dataset_key=dataset,
            translate=translate, handles=handles, ignore=_IGNORE,
        ))
