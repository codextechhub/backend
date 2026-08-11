"""Administration datasets published to the Export Centre.

Registered from :meth:`vs_user.apps.VsUserConfig.ready`. All tenant-scoped: people and
their access belong to the organisation, not to a set of books.

Everything here is about *who can do what*, which is why the columns that identify a
person - email, phone, date of birth - are restricted even though the row itself is
readable. An access review needs the roster; it does not need everyone's phone number.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATETIME,
    KIND_TEXT,
    Dataset,
    DatasetScope,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the tenant-scoped base queryset for user accounts.
def _users(scope):
    from .models import User

    return User.objects.filter(tenant=scope.tenant)


# Build the tenant-scoped base queryset for role assignments.
def _role_assignments(scope):
    from vs_rbac.models import TenantUserRoleAssignment

    return TenantUserRoleAssignment.objects.filter(tenant=scope.tenant)


# Build the tenant-scoped base queryset for sign-in sessions.
def _login_sessions(scope):
    from .models import LoginSession

    # all_objects: the default manager is tenant-aware and would double-scope.
    return LoginSession.all_objects.filter(tenant=scope.tenant)


_USER_TYPE = choice_labels("vs_user.models.User.UserType")
_USER_STATUS = choice_labels("vs_user.models.User.Status")


# Register every administration dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="admin.users",
        module="Administration",
        name="User accounts",
        description=(
            "Everyone with an account in this organisation, with their type and "
            "status. The roster an access review starts from. Contact columns are "
            "restricted."
        ),
        base=_users,
        scope=DatasetScope.TENANT,
        permission="platform.team.view",
        row_cap=100_000,
        default_columns=("email", "first_name", "last_name", "user_type", "status"),
        fields=(
            Field("email", "Email", "Account", KIND_TEXT, locked=True,
                  description="The account's identity - always exported."),
            Field("first_name", "First name", "Person", KIND_TEXT),
            Field("last_name", "Last name", "Person", KIND_TEXT),
            Field("user_type", "Type", "Account", KIND_CHOICE, choices=_USER_TYPE),
            Field("status", "Status", "Account", KIND_CHOICE, choices=_USER_STATUS),
            Field("is_active", "Active", "Account", KIND_TEXT),
            Field("last_login", "Last signed in", "Account", KIND_DATETIME),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("phone", "Phone", "Person", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("user_type", "Type", FILTER_CHOICE, choices=_USER_TYPE),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_USER_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("email", "Email"), ("first_name", "First name"),
                ("last_name", "Last name"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("email", "Email", FILTER_TEXT),
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
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the user list screen's filters into export filters.
def _translate_users(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    if value := params.get("user_type"):
        filters.append({"id": "user_type", "values": [value]})
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
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


# Register the administration screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="admin.users",
        handles=(
            "user_type", "status", "q", "search", "branch", "role",
        ),
        label="Administration - Users",
        dataset_key="admin.users",
        translate=_translate_users,
    ))
