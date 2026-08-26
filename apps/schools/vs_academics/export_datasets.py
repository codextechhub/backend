"""Academic structure datasets, published to the Export Centre.

Registered from :meth:`schools.vs_academics.apps.VsAcademicsConfig.ready`.
Registration lives here rather than in ``vs_exports`` so the engine never
imports a domain app, which is the pattern ``schools.vs_schools`` established.

**Every dataset here is tenant-fenced.** ``vs_schools`` carries the one dataset
in the platform that is not, and its module docstring records that as a reviewed
exception for the CX platform register. Nothing in this module is one: an
academic catalogue belongs to exactly one school.

**Branch narrowing is applied, and it is the same rule the screens use.** Each
catalogue dataset narrows through
:func:`vs_exports.catalogue.narrow_to_caller_branches`, which defers to
``vs_rbac.scoping.visible_branch_ids`` - the platform's one authority on which
branches a caller may see. So an export and the list it mirrors cannot answer
differently, and a branch admin can hold an export key and get their own
branch's data rather than being kept away from the feature.

The reading here is **inclusive**: a row with no branch is shared by the whole
school and everyone sees it. That is right for a catalogue, where the shared
rows are most of it - the exclusive reading would hand a branch user a nearly
empty file whenever the school published at school level, which is the normal
case. ``vs_procurement`` takes the opposite reading for its documents, and is
right to.

Sessions are not narrowed. A school year applies to the whole school or to a
named set of branches, and that is a many-to-many rather than a column, so the
helper does not fit it; it is a small dataset a school-level key already gates,
and narrowing it is left until somebody asks for it rather than guessed at.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_BOOLEAN,
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
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

_MODULE = "Academics"
_SESSION_STATUS = choice_labels("schools.vs_academics.models.SessionStatus")


def _sessions(scope):
    from .models import AcademicSession

    return AcademicSession.all_objects.filter(tenant=scope.tenant)


def _departments(scope):
    from .models import Department

    return narrow_to_caller_branches(
        Department.all_objects.filter(tenant=scope.tenant), scope,
    )


def _programs(scope):
    from .models import Program

    return narrow_to_caller_branches(
        Program.all_objects.filter(tenant=scope.tenant), scope,
    )


def _levels(scope):
    from .models import Level

    return narrow_to_caller_branches(
        Level.all_objects.filter(tenant=scope.tenant), scope,
    )


def _classes(scope):
    from .models import SchoolClass

    return narrow_to_caller_branches(
        SchoolClass.all_objects.filter(tenant=scope.tenant), scope,
    )


def _subjects(scope):
    from .models import Subject

    return narrow_to_caller_branches(
        Subject.all_objects.filter(tenant=scope.tenant), scope,
    )


#: The year a per-year row belongs to.
#:
#: Levels, classes and subjects each belong to exactly one, so a file without
#: this column silently stacked three years of JSS1 A on top of each other and
#: gave the reader no way to tell them apart.
_YEAR_FIELD = Field(
    "session__name", "Academic year", "Year", KIND_TEXT,
    description="The year this row belongs to.",
)

#: The screen sends a session ID, so the filter is on the id, not the name -
#: two years may not share a name at one school, but a name is still the wrong
#: thing to round-trip an id through.
_YEAR_FILTER = FilterDef("session_id", "Academic year", FILTER_CHOICE)

#: The scope chip every catalogue row carries, spelled once.
_SCOPE_FIELD = Field(
    "branch__name", "Branch", "Scope", KIND_TEXT,
    description="Blank where the item is shared by the whole school.",
)
_SCOPE_FILTER = FilterDef("branch__name", "Branch", FILTER_TEXT)
_ACTIVE_FILTER = FilterDef("is_active", "Active", FILTER_BOOLEAN)


def register_datasets():
    """Called once from AppConfig.ready()."""

    register(Dataset(
        key="academics.sessions",
        module=_MODULE,
        name="Academic sessions",
        description=(
            "The school years this school has defined, with their dates and "
            "status. One row per session, not per term."
        ),
        base=_sessions,
        scope=DatasetScope.TENANT,
        permission="academics.session.view",
        row_cap=10_000,
        default_columns=("name", "start_date", "end_date", "status"),
        fields=(
            Field("name", "Session", "Session", KIND_TEXT, locked=True),
            Field("start_date", "Starts", "Session", KIND_DATE),
            Field("end_date", "Ends", "Session", KIND_DATE),
            Field("status", "Status", "Session", KIND_CHOICE, choices=_SESSION_STATUS),
            Field("activated_at", "Activated", "Lifecycle", KIND_DATETIME),
            Field("archived_at", "Archived", "Lifecycle", KIND_DATETIME),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("start_date", "Starts", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_SESSION_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(("name", "Session"),)),
        ),
    ))

    register(Dataset(
        key="academics.departments",
        module=_MODULE,
        name="Departments",
        description="Faculty groupings, with the branch each belongs to.",
        base=_departments,
        scope=DatasetScope.TENANT,
        permission="academics.structure.view",
        row_cap=10_000,
        default_columns=("name", "code", "branch__name", "is_active"),
        fields=(
            Field("name", "Department", "Department", KIND_TEXT, locked=True),
            Field("code", "Code", "Department", KIND_TEXT),
            Field("description", "Description", "Department", KIND_TEXT),
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _SCOPE_FILTER,
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))

    register(Dataset(
        key="academics.programs",
        module=_MODULE,
        name="Programmes",
        description="The stages a pupil moves through, and the department each sits in.",
        base=_programs,
        scope=DatasetScope.TENANT,
        permission="academics.structure.view",
        row_cap=10_000,
        default_columns=("name", "code", "department__name", "branch__name"),
        fields=(
            Field("name", "Programme", "Programme", KIND_TEXT, locked=True),
            Field("code", "Code", "Programme", KIND_TEXT),
            Field("department__name", "Department", "Programme", KIND_TEXT),
            Field("order_index", "Order", "Programme", KIND_NUMBER),
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _SCOPE_FILTER,
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))

    register(Dataset(
        key="academics.levels",
        module=_MODULE,
        name="Levels",
        description=(
            "The year groups inside each programme, in progression order. One "
            "row per level, with the programme it belongs to."
        ),
        base=_levels,
        scope=DatasetScope.TENANT,
        permission="academics.structure.view",
        row_cap=20_000,
        default_columns=("name", "code", "program__name", "order_index"),
        fields=(
            Field("name", "Level", "Level", KIND_TEXT, locked=True),
            Field("code", "Code", "Level", KIND_TEXT),
            Field("program__name", "Programme", "Level", KIND_TEXT),
            Field("order_index", "Order", "Level", KIND_NUMBER),
            Field("next_level__name", "Promotes to", "Progression", KIND_TEXT,
                  description="Blank means the level is terminal, or that "
                              "promotion has not been wired yet."),
            _YEAR_FIELD,
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _YEAR_FILTER,
            _SCOPE_FILTER,
            FilterDef("program__name", "Programme", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))

    register(Dataset(
        key="academics.classes",
        module=_MODULE,
        name="Classes",
        description="The classes pupils sit in, with their level, arm and branch.",
        base=_classes,
        scope=DatasetScope.TENANT,
        permission="academics.classes.view",
        row_cap=50_000,
        default_columns=("name", "code", "level__name", "arm", "branch__name"),
        fields=(
            Field("name", "Class", "Class", KIND_TEXT, locked=True),
            Field("code", "Code", "Class", KIND_TEXT),
            Field("level__name", "Level", "Class", KIND_TEXT),
            Field("arm", "Arm", "Class", KIND_TEXT),
            Field("capacity", "Capacity", "Class", KIND_NUMBER,
                  description="Advisory here; enrolment enforces it."),
            _YEAR_FIELD,
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _YEAR_FILTER,
            _SCOPE_FILTER,
            FilterDef("level__name", "Level", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"), ("arm", "Arm"),
            )),
        ),
    ))

    register(Dataset(
        key="academics.subjects",
        module=_MODULE,
        name="Subjects",
        description=(
            "What the school teaches, whether each is core or elective, and the "
            "department it belongs to. Where a subject is offered is a separate "
            "fact per level and is not flattened into this row."
        ),
        base=_subjects,
        scope=DatasetScope.TENANT,
        permission="academics.subject.view",
        row_cap=20_000,
        default_columns=("name", "code", "department__name", "is_core"),
        fields=(
            Field("name", "Subject", "Subject", KIND_TEXT, locked=True),
            Field("code", "Code", "Subject", KIND_TEXT),
            Field("department__name", "Department", "Subject", KIND_TEXT),
            Field("is_core", "Core", "Subject", KIND_TEXT),
            Field("description", "Description", "Subject", KIND_TEXT),
            _YEAR_FIELD,
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _YEAR_FILTER,
            _SCOPE_FILTER,
            FilterDef("is_core", "Core", FILTER_BOOLEAN),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))


# ── Screen bindings ────────────────────────────────────────────────────────

"""Export what this table is showing.

The translation lives here rather than in vs_exports, because only this module
knows that ``is_active=all`` means "do not filter" and that ``status`` on the
sessions screen is a session status.

The branch lens is reported as UNMAPPED rather than carried, and that is the
honest answer rather than a shortcut. The lens means "school-wide rows PLUS
this branch's", which is an OR across a nullable column that no single
FilterDef expresses. Carrying it as ``branch__name contains X`` would silently
drop every school-wide row - most of a catalogue - and hand back a file
narrower than the screen with nothing to say so. Declared in ``handles`` and
returned unmapped, it sets ``exact`` false and puts the fact in front of the
reader before they run it.

The year lens IS carried: a row belongs to exactly one year, with no
shared-across-years case to widen it.

A branch-TIED caller needs none of this - the base querysets already narrow to
their branches, so their file matches their screen either way.
"""


#: What the screens send when a tri-state filter is switched off.
_NOT_FILTERING = ("", "all", "any")


def _flag(params, key, filters, unmapped):
    """Carry a screen's tri-state filter, or report that it could not be.

    Three outcomes, and the third is the one worth spelling out. "true"/"false"
    is carried; "all" is the screen NOT filtering, so there is nothing to carry
    and nothing to report; anything else is a value this translator does not
    understand, and it is REPORTED rather than ignored.

    Ignoring it is the silent widening the whole design exists to stop: the
    param is listed in `handles`, so `resolve_screen` would count it as carried
    and tell the reader their filter was applied when nothing was.
    """
    from vs_exports.catalogue import Unmapped

    raw = str(params.get(key, "")).strip()
    value = raw.lower()
    if value in ("true", "1"):
        filters.append({"id": key, "value": True})
    elif value in ("false", "0"):
        filters.append({"id": key, "value": False})
    elif value not in _NOT_FILTERING:
        unmapped.append(Unmapped(
            key, raw,
            f"The export does not understand “{raw}” for this filter, so the "
            f"file is not limited by it.",
        ))


#: Why the branch lens cannot be carried, in the words the drawer shows.
_BRANCH_REASON = (
    "The branch you are viewing also includes everything shared by the whole "
    "school, which an export filter cannot express. The file covers every branch "
    "you can see."
)


def _year(params, filters):
    """Carry the year lens into the export.

    Unlike the branch lens this one CAN be expressed as a filter: a row belongs
    to exactly one year, with no shared-across-all-years case to widen it. A
    screen showing 2025/2026 that exported every year was handing a school a
    file three times the size of what it was looking at.
    """
    session = str(params.get("session", "")).strip()
    if session:
        filters.append({"id": "session_id", "value": [session]})


def _common(params):
    """search, is_active and the branch lens - every catalogue screen sends these."""
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    search = str(params.get("search", "")).strip()
    if search:
        filters.append({"id": "search", "value": search})

    _flag(params, "is_active", filters, unmapped)

    branch = str(params.get("branch", "")).strip()
    if branch:
        unmapped.append(Unmapped("branch", branch, _BRANCH_REASON))
    return filters, unmapped


def _translate_catalogue(params):
    return _common(params)


def _translate_per_year(params):
    """A catalogue screen whose rows belong to one year."""
    filters, unmapped = _common(params)
    _year(params, filters)
    return filters, unmapped


def _translate_classes(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = _common(params)
    _year(params, filters)
    # The screen filters by level id, the dataset by level name, and there is
    # no tenant here to resolve one into the other. Reported, not guessed.
    level = str(params.get("level", "")).strip()
    if level:
        unmapped.append(Unmapped(
            "level", level,
            "The export cannot filter by the level you picked, so the file "
            "covers every level.",
        ))
    return filters, unmapped


def _translate_subjects(params):
    filters, unmapped = _common(params)
    _year(params, filters)
    _flag(params, "is_core", filters, unmapped)
    return filters, unmapped


def _translate_sessions(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    search = str(params.get("search", "")).strip()
    if search:
        filters.append({"id": "search", "value": search})
    status = str(params.get("status", "")).strip()
    if status and status.upper() not in ("ALL", "ANY"):
        if status.upper() in _SESSION_STATUS:
            filters.append({"id": "status", "value": [status.upper()]})
        else:
            unmapped.append(Unmapped(
                "status", status,
                f"“{status}” is not a session status the export knows, so the "
                f"file is not limited by it.",
            ))
    branch = str(params.get("branch", "")).strip()
    if branch:
        unmapped.append(Unmapped("branch", branch, _BRANCH_REASON))
    return filters, unmapped


#: Params every academics screen carries that are not filters. Listed so they
#: are not reported as dropped: a page number is not a narrowing.
_IGNORE = ("page", "page_size", "tenant", "view")


def register_screens():
    """Called once from AppConfig.ready(), after register_datasets()."""
    from vs_exports.catalogue import ScreenBinding, register_screen

    for key, label, dataset, translate, handles in (
        ("academics.sessions", "Academics - Sessions & terms", "academics.sessions",
         _translate_sessions, ("search", "status", "branch")),
        ("academics.departments", "Academics - Departments", "academics.departments",
         _translate_catalogue, ("search", "is_active", "branch")),
        ("academics.programs", "Academics - Programmes", "academics.programs",
         _translate_catalogue, ("search", "is_active", "branch")),
        ("academics.levels", "Academics - Levels", "academics.levels",
         _translate_per_year, ("search", "is_active", "branch", "session")),
        ("academics.classes", "Academics - Classes & arms", "academics.classes",
         _translate_classes, ("search", "is_active", "branch", "level", "session")),
        ("academics.subjects", "Academics - Subjects", "academics.subjects",
         _translate_subjects, ("search", "is_active", "branch", "is_core", "session")),
    ):
        register_screen(ScreenBinding(
            key=key, label=label, dataset_key=dataset,
            translate=translate, handles=handles, ignore=_IGNORE,
        ))
