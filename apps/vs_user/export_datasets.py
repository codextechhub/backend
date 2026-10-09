"""Administration datasets published to the Export Centre.

Registered from :meth:`vs_user.apps.VsUserConfig.ready`. Account rows are tenant-scoped;
the organogram is platform-owned and has no branch dimension.

Everything here is about *who can do what*, which is why the columns that identify a
person - email, phone, date of birth - are restricted even though the row itself is
readable. An access review needs the roster; it does not need everyone's phone number.
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
    register,
)


def _users(scope):
    """Accounts owned by the caller's tenant, with portable import references."""
    from django.db.models import OuterRef, Subquery
    from vs_rbac.models import TenantUserRoleAssignment
    from .models import User

    active_role = TenantUserRoleAssignment.objects.filter(
        user_id=OuterRef("pk"), tenant=scope.tenant,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).order_by("pk")
    return (
        User.objects.filter(tenant=scope.tenant)
        .exclude(status__in=[User.Status.PENDING_APPROVAL, User.Status.REJECTED])
        .annotate(_active_role_key=Subquery(active_role.values("role__key")[:1]))
    )


def _tenant_users(scope):
    """Accounts belonging to customer tenants, for the platform directory."""
    from vs_tenants.models import Tenant
    from .models import User

    return User.objects.filter(tenant__kind=Tenant.Kind.SCHOOL).exclude(
        status__in=[User.Status.PENDING_APPROVAL, User.Status.REJECTED],
    )


def _org_units(scope):
    from django.db.models import Case, IntegerField, Value, When
    from .models import OrgNode

    return OrgNode.objects.annotate(
        _portable_order=Case(
            When(kind=OrgNode.Kind.DIVISION, then=Value(0)),
            When(kind=OrgNode.Kind.DEPARTMENT, then=Value(1)),
            When(kind=OrgNode.Kind.TEAM, then=Value(2)),
            default=Value(3), output_field=IntegerField(),
        )
    ).order_by("_portable_order", "code")


def _positions(scope):
    """Positions ordered so every solid-line manager precedes its reports."""
    from django.db.models import Case, IntegerField, Value, When
    from .models import Position

    links = list(Position.objects.values_list("pk", "reports_to_id", "code"))
    remaining = {pk: (manager_id, code) for pk, manager_id, code in links}
    emitted = set()
    ordered = []
    while remaining:
        ready = sorted(
            (
                (code, pk) for pk, (manager_id, code) in remaining.items()
                if manager_id is None or manager_id in emitted or manager_id not in remaining
            ),
        )
        if not ready:
            ready = sorted((code, pk) for pk, (_manager_id, code) in remaining.items())
        for _code, pk in ready:
            ordered.append(pk)
            emitted.add(pk)
            remaining.pop(pk)
    cases = [When(pk=pk, then=Value(index)) for index, pk in enumerate(ordered)]
    return Position.objects.annotate(
        _portable_order=Case(*cases, default=Value(len(ordered)), output_field=IntegerField())
    ).order_by("_portable_order", "code")


def _matrix_reports(scope):
    from .models import MatrixReport

    return MatrixReport.objects.all()


# Build the tenant-scoped base queryset for role assignments.
def _role_assignments(scope):
    from vs_rbac.models import TenantUserRoleAssignment

    return TenantUserRoleAssignment.objects.filter(tenant=scope.tenant)


# Build the tenant-scoped base queryset for sign-in sessions.
def _login_sessions(scope):
    from .models import LoginSession

    # all_objects: the default manager is tenant-aware and would double-scope.
    return LoginSession.all_objects.filter(tenant=scope.tenant)


_TENANT_KIND = choice_labels("vs_tenants.models.Tenant.Kind")
_USER_STATUS = choice_labels("vs_user.models.User.Status")
_USER_GENDER = choice_labels("vs_user.models.User.Gender")
_EMPLOYMENT_TYPE = choice_labels("vs_user.models.PlatformStaffProfile.EmploymentType")
_ORG_KIND = choice_labels("vs_user.models.OrgNode.Kind")


# Register every administration dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="admin.users",
        module="Administration",
        name="User accounts",
        description=(
            "Accounts owned by your tenant. In system values mode the default "
            "columns match the CX users import template. Contact columns remain "
            "restricted."
        ),
        base=_users,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        row_cap=100_000,
        default_columns=(
            "first_name", "last_name", "role_key", "phone", "gender",
            "employment_type", "position_code", "date_joined",
        ),
        fields=(
            Field("email", "Email", "Account", KIND_TEXT, locked=True,
                  description="The account's identity - always exported."),
            Field("first_name", "First Name", "Person", KIND_TEXT,
                  access="platform.staff_profile.first_name"),
            Field("last_name", "Last Name", "Person", KIND_TEXT,
                  access="platform.staff_profile.last_name"),
            Field("role_key", "Role Key", "Account", KIND_TEXT,
                  source="_active_role_key"),
            Field("role", "Role", "Account", KIND_TEXT),
            Field("status", "Status", "Account", KIND_CHOICE, choices=_USER_STATUS),
            Field("is_active", "Active", "Account", KIND_TEXT),
            Field("last_login", "Last signed in", "Account", KIND_DATETIME),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("phone", "Phone", "Person", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
            Field("gender", "Gender", "Person", KIND_CHOICE, choices=_USER_GENDER),
            Field("employment_type", "Employment Type", "Employment", KIND_CHOICE,
                  source="platform_staff_profile__employment_type",
                  choices=_EMPLOYMENT_TYPE,
                  access="platform.staff_profile.employment_type"),
            Field("position_code", "Position", "Employment", KIND_TEXT,
                  source="platform_staff_profile__position__code",
                  access="platform.staff_profile.job_title"),
            Field("date_joined", "Date Joined", "Employment", KIND_DATE,
                  source="platform_staff_profile__date_joined",
                  access="platform.staff_profile.date_joined"),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_USER_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("email", "Email"), ("first_name", "First name"),
                ("last_name", "Last name"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("email", "Email", FILTER_TEXT),
        ),
    ))

    register(Dataset(
        key="admin.school_users",
        module="Administration",
        name="School user accounts",
        description=(
            "Accounts belonging to school tenants, with the school name and code "
            "needed to identify each row. Available only in the platform console."
        ),
        base=_tenant_users,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        tenant_kinds=("PLATFORM",),
        row_cap=100_000,
        default_columns=(
            "email", "first_name", "last_name", "role", "status", "school",
            "school_code",
        ),
        fields=(
            Field("email", "Email", "Account", KIND_TEXT, locked=True),
            Field("first_name", "First name", "Person", KIND_TEXT),
            Field("last_name", "Last name", "Person", KIND_TEXT),
            Field("role", "Role", "Account", KIND_TEXT),
            Field("status", "Status", "Account", KIND_CHOICE, choices=_USER_STATUS),
            Field("is_active", "Active", "Account", KIND_TEXT),
            Field("phone", "Phone", "Person", KIND_TEXT, sensitive=True),
            Field("school", "School", "Placement", KIND_TEXT,
                  source="tenant__school_profile__name"),
            Field("school_code", "School code", "Placement", KIND_TEXT,
                  source="tenant__school_profile__code"),
            Field("tenant_kind", "Tenant type", "Placement", KIND_CHOICE,
                  source="tenant__kind", choices=_TENANT_KIND),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_USER_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("email", "Email"), ("first_name", "First name"),
                ("last_name", "Last name"),
            )),
            FilterDef("school", "School", FILTER_TEXT,
                      source="tenant__school_profile__name"),
        ),
    ))

    register(Dataset(
        key="admin.org_units",
        module="Administration",
        name="Organogram units",
        description="CodeX divisions, departments and teams, using portable codes.",
        base=_org_units,
        scope=DatasetScope.TENANT,
        permission="platform.organogram.view",
        tenant_kinds=("PLATFORM",),
        row_cap=10_000,
        default_columns=("name", "kind", "parent_code", "description", "is_active"),
        fields=(
            Field("code", "Code", "Org unit", KIND_TEXT, locked=True),
            Field("name", "Name", "Org unit", KIND_TEXT),
            Field("kind", "Kind", "Org unit", KIND_CHOICE, choices=_ORG_KIND),
            Field("parent_code", "Parent Code", "Hierarchy", KIND_TEXT,
                  source="parent__code"),
            Field("description", "Description", "Org unit", KIND_TEXT),
            Field("is_active", "Active", "Org unit", KIND_TEXT),
        ),
    ))

    register(Dataset(
        key="admin.positions",
        module="Administration",
        name="Organogram positions",
        description="CodeX seats and solid reporting lines, using portable codes.",
        base=_positions,
        scope=DatasetScope.TENANT,
        permission="platform.organogram.view",
        tenant_kinds=("PLATFORM",),
        row_cap=20_000,
        default_columns=(
            "title", "org_unit_code", "reports_to_code", "default_role_key",
            "headcount", "is_active",
        ),
        fields=(
            Field("code", "Code", "Position", KIND_TEXT, locked=True),
            Field("title", "Title", "Position", KIND_TEXT),
            Field("org_unit_code", "Org Unit Code", "Hierarchy", KIND_TEXT,
                  source="org_node__code"),
            Field("reports_to_code", "Reports To Code", "Hierarchy", KIND_TEXT,
                  source="reports_to__code"),
            Field("default_role_key", "Default Role Key", "Access", KIND_TEXT,
                  source="default_role__key"),
            Field("headcount", "Headcount", "Position", KIND_NUMBER),
            Field("is_active", "Active", "Position", KIND_TEXT),
        ),
    ))

    register(Dataset(
        key="admin.matrix_reports",
        module="Administration",
        name="Matrix reporting lines",
        description="CodeX dotted reporting lines, using portable position codes.",
        base=_matrix_reports,
        scope=DatasetScope.TENANT,
        permission="platform.organogram.view",
        tenant_kinds=("PLATFORM",),
        row_cap=20_000,
        default_columns=("reports_to_code", "relationship_label"),
        fields=(
            Field("position_code", "Position Code", "Reporting line", KIND_TEXT,
                  source="position__code", locked=True),
            Field("reports_to_code", "Reports To Code", "Reporting line", KIND_TEXT,
                  source="reports_to__code"),
            Field("relationship_label", "Relationship Label", "Reporting line",
                  KIND_TEXT),
        ),
    ))

    register(Dataset(
        key="admin.role_assignments",
        module="Administration",
        name="Role assignments",
        description=(
            "Who holds which role, when it was granted and by whom - including "
            "revoked grants. The answer to 'who could approve payments last March'."
        ),
        base=_role_assignments,
        scope=DatasetScope.TENANT,
        permission="platform.roles.view",
        row_cap=200_000,
        default_columns=("user_email", "role_name", "assignment_status", "assigned_at"),
        fields=(
            Field("assignment_id", "Assignment", "Assignment", KIND_TEXT, source="id",
                  locked=True),
            Field("user_email", "User", "Assignment", KIND_TEXT, source="user__email"),
            Field("role_key", "Role key", "Assignment", KIND_TEXT, source="role__key"),
            Field("role_name", "Role", "Assignment", KIND_TEXT, source="role__name"),
            Field("assignment_status", "Status", "Assignment", KIND_TEXT),
            Field("assigned_at", "Granted", "Assignment", KIND_DATETIME),
            Field("assigned_by", "Granted by", "Assignment", KIND_TEXT,
                  source="assigned_by__email"),
            Field("revoked_at", "Revoked", "Assignment", KIND_DATETIME),
            Field("revoked_by", "Revoked by", "Assignment", KIND_TEXT,
                  source="revoked_by__email"),
        ),
        filters=(
            FilterDef("assigned_at", "Granted", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("assignment_status", "Status", FILTER_TEXT),
            FilterDef("role", "Role", FILTER_TEXT, source="role__name"),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("user__email", "User"),
                ("role__name", "Role"),
                ("role__key", "Role key"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))

    register(Dataset(
        key="admin.sign_ins",
        module="Administration",
        name="Sign-in sessions",
        description=(
            "Every session opened against this organisation, with device and outcome. "
            "IP address and user agent are restricted."
        ),
        base=_login_sessions,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        row_cap=500_000,
        default_columns=("user_email", "last_seen_at", "is_active"),
        fields=(
            Field("session_id", "Session", "Session", KIND_TEXT, source="id", locked=True),
            Field("user_email", "User", "Session", KIND_TEXT, source="user__email"),
            Field("last_seen_at", "Last seen", "Session", KIND_DATETIME),
            Field("ended_at", "Ended", "Session", KIND_DATETIME),
            Field("end_reason", "Why it ended", "Session", KIND_TEXT),
            Field("is_active", "Still active", "Session", KIND_TEXT),
            Field("device_label", "Device label", "Device", KIND_TEXT),
            Field("ip_address", "IP address", "Device", KIND_TEXT, sensitive=True,
                  description="Restricted: identifies where a person signed in from."),
            Field("user_agent", "Device", "Device", KIND_TEXT, sensitive=True,
                  description="Restricted: identifies a person's device."),
        ),
        filters=(
            FilterDef("last_seen_at", "Last seen", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("user", "User", FILTER_TEXT, source="user__email"),
            FilterDef("is_active", "Still active", FILTER_BOOLEAN),
            FilterDef("end_reason", "Why it ended", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("user__email", "User"),
                ("device_label", "Device label"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the user list screen's filters into export filters.
def _translate_users(params):
    from vs_exports.catalogue import Unmapped

    from .models import User

    filters, unmapped = [], []
    if scope := params.get("scope"):
        if scope not in {"cx", "school"}:
            unmapped.append(Unmapped(
                "scope", scope,
                "This view is not one the user export recognises, so the file is not "
                "limited by it.",
            ))
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    elif excluded := params.get("exclude_status"):
        # The Members tab hides drafts and unapproved accounts with
        # `exclude_status=PENDING,DRAFT`. The export filter is "is any of", so
        # the exclusion is carried as its complement. Without this the default
        # view of the busiest user screen in the console warns on every open.
        drop = {v.strip() for v in str(excluded).split(",") if v.strip()}
        # The base already withholds these two everywhere, so naming them in the
        # complement would put them in the readable summary of the file
        # ("Status is any of ... Creation Rejected") while the rows never appear.
        drop |= {str(User.Status.PENDING_APPROVAL), str(User.Status.REJECTED)}
        keep = [str(s) for s in User.Status.values if str(s) not in drop]
        if keep:
            filters.append({"id": "status", "values": keep})
        else:
            unmapped.append(Unmapped(
                "exclude_status", excluded,
                "This view excludes every status the export knows about, so it cannot "
                "be expressed as a filter.",
            ))
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    if (value := params.get("branch")) is not None:
        unmapped.append(Unmapped(
            "branch", value,
            "The user export does not filter by branch yet; the file covers the whole "
            "organisation.",
        ))
    if (value := params.get("role")) is not None:
        unmapped.append(Unmapped(
            "role", value,
            "Export Role assignments instead - that dataset is per grant, so it can "
            "filter by role.",
        ))
    return filters, unmapped


def _users_dataset(params):
    """Choose the tenant-owned or cross-tenant roster for the visible tab."""
    return "admin.school_users" if params.get("scope") == "school" else "admin.users"


# Translate the role-assignments screen's filters into export filters.
def _translate_role_assignments(params):
    filters, unmapped = [], []
    # The screen sends "all" to mean no filter, not a status called "all".
    if (value := params.get("assignment_status")) and value != "all":
        filters.append({"id": "assignment_status", "value": value})
    if (value := params.get("role")) and value != "all":
        filters.append({"id": "role", "value": value})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    return filters, unmapped


# Translate the sign-in sessions screen's filters into export filters.
def _translate_sign_ins(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("is_active"):
        filters.append({"id": "is_active", "value": str(value).lower() == "true"})
    if value := params.get("end_reason"):
        filters.append({"id": "end_reason", "value": value})
    if value := params.get("school"):
        unmapped.append(Unmapped(
            "school", value,
            "The sign-ins export does not carry a school filter; the file covers every "
            "school the other filters allow.",
        ))
    if value := params.get("ended_today"):
        unmapped.append(Unmapped(
            "ended_today", value,
            "The export filters on when a session was last seen, not on when it ended. "
            "Set the Last seen range in the builder to narrow it.",
        ))
    return filters, unmapped


# Register the administration screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="admin.users",
        handles=(
            "scope", "status", "exclude_status", "q", "search",
            "branch", "role",
        ),
        label="Administration - Users",
        dataset_key="admin.users",
        dataset_from_params=_users_dataset,
        translate=_translate_users,
    ))
    register_screen(ScreenBinding(
        key="admin.role_assignments",
        handles=(
            "assignment_status", "role", "search",
        ),
        label="Administration - Role assignments",
        dataset_key="admin.role_assignments",
        translate=_translate_role_assignments,
    ))
    register_screen(ScreenBinding(
        key="admin.sign_ins",
        handles=(
            "search", "is_active", "end_reason", "school", "ended_today",
        ),
        label="Administration - Sign-in sessions",
        dataset_key="admin.sign_ins",
        translate=_translate_sign_ins,
        # A security review looks at the recent past, and sessions are high
        # volume; 90 days matches the audit console.
        default_window_days=90,
    ))
