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
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
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
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
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
            _SCOPE_FIELD,
            Field("is_active", "Active", "Record", KIND_TEXT),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            _ACTIVE_FILTER,
            _SCOPE_FILTER,
            FilterDef("is_core", "Core", FILTER_BOOLEAN),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("name", "Name"), ("code", "Code"),
            )),
        ),
    ))
